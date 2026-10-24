"""One charge point: its state, its commands, and its OCPP message handling."""

# The OCPP handler is private to this module and works on its owner's state.
# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import count
from types import MappingProxyType
from typing import Any, TypeVar

from aiohttp import WSMsgType, web
from ocpp.exceptions import OCPPError, UnknownCallErrorCodeError
from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.datatypes import IdTagInfo
from ocpp.v16.enums import Action

from .const import (
    DEFAULT_BOOT_RETRY_INTERVAL,
    AuthorizationStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ConnectorStatus,
    DataTransferStatus,
    MessageTrigger,
    RegistrationStatus,
    ResetType,
)
from .exceptions import CsmsCallError, CsmsConnectionError, CsmsTimeoutError
from .models import (
    DEFAULT_MEASURAND,
    DEFAULT_UNIT,
    ENERGY_MEASURAND_PREFIX,
    BootEvent,
    BootInfo,
    ChargePointEvent,
    Configuration,
    ConfigurationValue,
    ConnectedEvent,
    ConnectorState,
    DataTransferResult,
    DiagnosticsStatusEvent,
    DisconnectedEvent,
    FirmwareStatusEvent,
    MeterReading,
    MeterValuesEvent,
    SecurityEvent,
    StatusEvent,
    Transaction,
    TransactionStartedEvent,
    TransactionStoppedEvent,
)

_LOGGER = logging.getLogger(__package__)

_WIRE_LOGGER = logging.getLogger(f"{__package__}.wire")
"""Logs every OCPP message in full, credentials included. Debug level only."""
if _WIRE_LOGGER.level == logging.NOTSET:
    # Debug logging for the package must not switch this on with it: it has
    # to be asked for by name.
    _WIRE_LOGGER.setLevel(logging.INFO)

EventListener = Callable[[ChargePointEvent], None]
"""Called with each event. Runs inside message handling, so it must not
block, and must schedule rather than await any request to the charge
point."""

RegistrationHook = Callable[["ChargePoint", BootInfo], Awaitable[RegistrationStatus]]
"""Decides the answer to a ``BootNotification``. Runs inside message
handling, so it must not await a request to the same charge point."""

AuthorizeHook = Callable[["ChargePoint", str], Awaitable[AuthorizationStatus]]
"""Decides whether an id tag may charge, for ``Authorize`` and
``StartTransaction``. Runs inside message handling, so it must not await a
request to the same charge point."""

DataTransferHandler = Callable[
    ["ChargePoint", str | None, str | None],
    Awaitable[tuple[DataTransferStatus, str | None]],
]
"""Answers a ``DataTransfer`` from a charge point.

Called with the charge point, the ``messageId`` and the ``data``. Returns the
reply's ``status`` and ``data``. Runs inside message handling, so it must not
await a request to the same charge point."""

_HandlerT = TypeVar("_HandlerT", bound=Callable[..., Any])


def _on(action: str) -> Callable[[_HandlerT], _HandlerT]:
    """Typed :func:`ocpp.routing.on`."""
    decorator: Callable[[_HandlerT], _HandlerT] = on(action)
    return decorator


def _now() -> str:
    """Current time in the form charge points expect."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str | None) -> datetime | None:
    """Parse an OCPP timestamp, or return ``None`` if it is missing or
    malformed."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_readings(meter_values: Iterable[dict[str, Any]]) -> tuple[MeterReading, ...]:
    """Flatten OCPP ``meterValue`` entries into readings, oldest first."""
    readings = []
    for meter_value in meter_values:
        timestamp = _parse_time(meter_value.get("timestamp"))
        for sample in meter_value.get("sampled_value", ()):
            measurand = sample.get("measurand", DEFAULT_MEASURAND)
            default_unit = (
                DEFAULT_UNIT if measurand.startswith(ENERGY_MEASURAND_PREFIX) else None
            )
            readings.append(
                MeterReading(
                    value=sample["value"],
                    measurand=measurand,
                    unit=sample.get("unit", default_unit),
                    phase=sample.get("phase"),
                    location=sample.get("location"),
                    context=sample.get("context"),
                    format=sample.get("format"),
                    timestamp=timestamp,
                )
            )
    return tuple(readings)


class _WireLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Keeps the ``ocpp`` package's messages, which embed payloads, at debug
    level.

    Warnings and errors still surface on the package logger, without the
    payload.
    """

    def log(self, level: int, msg: object, *args: object, **kwargs: Any) -> None:
        """Log ``msg`` at debug level, and a payload free note at ``level``
        if that is warning or above."""
        if level >= logging.WARNING:
            _LOGGER.log(
                level,
                "OCPP message from or to charge point %s failed; the %s logger "
                "has details at debug level",
                (self.extra or {}).get("charge_point_id"),
                self.logger.name,
            )
        self.logger.debug(msg, *args, **kwargs)


class _ConnectionClosed(Exception):
    """The WebSocket closed."""


class _WebSocketConnection:
    """The ``recv``/``send`` interface the ``ocpp`` package reads and writes."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws

    async def recv(self) -> str:
        """Return the next text message.

        :raises _ConnectionClosed: once the WebSocket closes.
        """
        while True:
            msg = await self._ws.receive()
            if msg.type is WSMsgType.TEXT:
                return str(msg.data)
            if msg.type is WSMsgType.BINARY:
                # OCPP-J is text only.
                continue
            raise _ConnectionClosed

    async def send(self, message: str) -> None:
        """Send a text message."""
        await self._ws.send_str(message)


@dataclass(slots=True)
class _Settings:
    """Central system settings each charge point applies."""

    heartbeat_interval: int
    call_timeout: float
    registration: RegistrationHook | None
    authorize: AuthorizeHook | None
    data_transfer_handlers: dict[str, DataTransferHandler]
    emit: Callable[[ChargePointEvent], None]
    transaction_ids: Iterator[int] = field(
        default_factory=lambda: count(int(datetime.now(UTC).timestamp()))
    )


class _Session:
    """One WebSocket connection of a charge point."""

    def __init__(
        self, ws: web.WebSocketResponse, handler: _OcppHandler, timeout: float
    ) -> None:
        self.ws = ws
        self.handler = handler
        self.timeout = timeout
        self.closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def async_call(self, request: Any) -> Any:
        """Send ``request`` and return the charge point's answer.

        The timeout covers waiting for earlier requests as well as this one.
        """
        name = type(request).__name__
        task = asyncio.ensure_future(self.handler.call(request, suppress=False))
        try:
            await asyncio.wait(
                (task, self.closed),
                timeout=self.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not task.done():
                task.cancel()
        if self.closed.done() and not task.done():
            raise CsmsConnectionError(
                f"connection closed before the charge point answered {name}"
            )
        if not task.done() or task.cancelled():
            raise CsmsTimeoutError(f"no answer to {name} within {self.timeout} s")
        try:
            return task.result()
        except TimeoutError as err:
            raise CsmsTimeoutError(
                f"no answer to {name} within {self.timeout} s"
            ) from err
        except ConnectionError as err:
            raise CsmsConnectionError(f"sending {name} failed: {err}") from err
        except OCPPError as err:
            raise CsmsCallError(
                getattr(err, "code", type(err).__name__), str(err.description)
            ) from err
        except UnknownCallErrorCodeError as err:
            raise CsmsCallError("Unknown", str(err)) from err

    def mark_closed(self) -> None:
        """Fail requests still waiting for an answer."""
        if not self.closed.done():
            self.closed.set_result(None)


class ChargePoint:
    """A charge point known to the central system.

    One object exists per charge point id for the life of the
    :class:`~ocpp_csms.CentralSystem`, and keeps its state across
    reconnections. Get it from :attr:`CentralSystem.charge_points
    <ocpp_csms.CentralSystem.charge_points>`, typically on a
    :class:`~ocpp_csms.ConnectedEvent`.

    State attributes hold the last values the charge point reported. Request
    methods send an OCPP request and return the charge point's answer. They
    raise :class:`~ocpp_csms.CsmsConnectionError` if the charge point is not
    connected or drops before answering,
    :class:`~ocpp_csms.CsmsTimeoutError` if it does not answer in time, and
    :class:`~ocpp_csms.CsmsCallError` if it answers with an OCPP error.
    Requests to one charge point are sent one at a time.
    """

    def __init__(self, charge_point_id: str, settings: _Settings) -> None:
        """Create the charge point. The central system does this."""
        self._id = charge_point_id
        self._settings = settings
        self._session: _Session | None = None
        self._closing: set[asyncio.Task[bool]] = set()
        self._listeners: list[EventListener] = []
        self._boot: BootInfo | None = None
        self._registration_status: RegistrationStatus | None = None
        self._connectors: dict[int, ConnectorState] = {}
        self._meter: dict[
            int, dict[tuple[str, str | None, str | None], MeterReading]
        ] = {}
        self._transactions: dict[int, Transaction] = {}
        self._last_seen: datetime | None = None
        self._firmware_status: str | None = None
        self._diagnostics_status: str | None = None

    def __repr__(self) -> str:
        """Show the id and whether it is connected."""
        state = "connected" if self.connected else "disconnected"
        return f"<ChargePoint {self._id!r} {state}>"

    @property
    def id(self) -> str:
        """Charge point id: the last segment of the URL it connects to."""
        return self._id

    @property
    def connected(self) -> bool:
        """True while the charge point's WebSocket is open."""
        return self._session is not None

    @property
    def boot(self) -> BootInfo | None:
        """What the charge point said in its last ``BootNotification``."""
        return self._boot

    @property
    def registration_status(self) -> RegistrationStatus | None:
        """Answer given to the last ``BootNotification``."""
        return self._registration_status

    @property
    def connectors(self) -> Mapping[int, ConnectorState]:
        """Last status of each connector, by connector id. Connector 0 is the
        charge point as a whole."""
        return MappingProxyType(self._connectors)

    @property
    def transactions(self) -> Mapping[int, Transaction]:
        """Running sessions, by transaction id."""
        return MappingProxyType(self._transactions)

    def get_transaction(self, connector_id: int) -> Transaction | None:
        """Running session on a connector, the latest if there are several,
        or ``None``."""
        for transaction in reversed(self._transactions.values()):
            if transaction.connector_id == connector_id:
                return transaction
        return None

    @property
    def last_seen(self) -> datetime | None:
        """When the charge point last sent a message."""
        return self._last_seen

    @property
    def firmware_status(self) -> str | None:
        """Last reported firmware update status."""
        return self._firmware_status

    @property
    def diagnostics_status(self) -> str | None:
        """Last reported diagnostics upload status."""
        return self._diagnostics_status

    def meter_readings(self, connector_id: int) -> tuple[MeterReading, ...]:
        """Latest reading of each measurand, phase and location on a
        connector."""
        return tuple(self._meter.get(connector_id, {}).values())

    def get_meter_reading(
        self,
        connector_id: int,
        measurand: str = DEFAULT_MEASURAND,
        phase: str | None = None,
        location: str | None = None,
    ) -> MeterReading | None:
        """Latest reading of one measurand on a connector, or ``None``."""
        return self._meter.get(connector_id, {}).get((measurand, phase, location))

    def add_listener(self, listener: EventListener) -> Callable[[], None]:
        """Call ``listener`` with each event from this charge point.

        Returns a function that removes the listener.
        """
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def async_call(self, request: Any) -> Any:
        """Send any OCPP 1.6 request and return the answer.

        ``request`` is a request object from ``ocpp.v16.call``, and the
        answer the matching object from ``ocpp.v16.call_result``. Use this
        for requests that have no method of their own here, such as
        ``SetChargingProfile``.
        """
        session = self._session
        if session is None:
            raise CsmsConnectionError(f"charge point {self._id} is not connected")
        return await session.async_call(request)

    async def async_data_transfer(
        self, vendor_id: str, message_id: str | None = None, data: str | None = None
    ) -> DataTransferResult:
        """Send a vendor specific ``DataTransfer``.

        The signature fits callers that pass ``vendor_id``, ``message_id``
        and ``data`` positionally and unpack ``status, data`` from the
        answer.
        """
        result = await self.async_call(
            call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=data)
        )
        return DataTransferResult(DataTransferStatus(result.status), result.data)

    async def async_remote_start_transaction(
        self, id_tag: str, connector_id: int | None = None
    ) -> str:
        """Ask the charge point to start a session for ``id_tag``.

        Returns ``Accepted`` or ``Rejected``. Accepted means the charge point
        will try; a :class:`~ocpp_csms.TransactionStartedEvent` follows once
        the session runs.
        """
        result = await self.async_call(
            call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id)
        )
        return str(result.status)

    async def async_remote_stop_transaction(self, transaction_id: int) -> str:
        """Ask the charge point to stop a session.

        Returns ``Accepted`` or ``Rejected``.
        """
        result = await self.async_call(
            call.RemoteStopTransaction(transaction_id=transaction_id)
        )
        return str(result.status)

    async def async_reset(self, reset_type: ResetType = ResetType.SOFT) -> str:
        """Ask the charge point to restart.

        Returns ``Accepted`` or ``Rejected``. The connection drops while the
        charge point restarts.
        """
        result = await self.async_call(call.Reset(type=reset_type.value))
        return str(result.status)

    async def async_change_availability(
        self, connector_id: int, availability: AvailabilityType
    ) -> str:
        """Take a connector out of service or return it, 0 for all.

        Returns ``Accepted``, ``Rejected`` or ``Scheduled``. Scheduled means
        the change applies once the running session ends.
        """
        result = await self.async_call(
            call.ChangeAvailability(connector_id=connector_id, type=availability.value)
        )
        return str(result.status)

    async def async_get_configuration(
        self, keys: Iterable[str] | None = None
    ) -> Configuration:
        """Read configuration keys, or every key if ``keys`` is ``None``."""
        key_list = list(keys) if keys is not None else None
        result = await self.async_call(call.GetConfiguration(key=key_list))
        values = {
            entry["key"]: ConfigurationValue(
                key=entry["key"],
                value=entry.get("value"),
                readonly=bool(entry["readonly"]),
            )
            for entry in result.configuration_key or ()
        }
        return Configuration(values, tuple(result.unknown_key or ()))

    async def async_change_configuration(self, key: str, value: str) -> str:
        """Change one configuration key.

        Returns ``Accepted``, ``Rejected``, ``RebootRequired`` or
        ``NotSupported``. A wrong value for a key such as ``CentralSystemURL``
        or ``AuthorizationKey`` leaves the charge point unable to reach this
        central system until it is re-provisioned locally.
        """
        result = await self.async_call(call.ChangeConfiguration(key=key, value=value))
        return str(result.status)

    async def async_trigger_message(
        self, message: MessageTrigger, connector_id: int | None = None
    ) -> str:
        """Ask the charge point to send a message now.

        Returns ``Accepted``, ``Rejected`` or ``NotImplemented``. The message
        itself arrives afterwards, as the matching event.
        """
        result = await self.async_call(
            call.TriggerMessage(
                requested_message=message.value, connector_id=connector_id
            )
        )
        return str(result.status)

    async def async_unlock_connector(self, connector_id: int) -> str:
        """Ask the charge point to unlock a connector.

        Returns ``Unlocked``, ``UnlockFailed`` or ``NotSupported``. A running
        session on the connector stops.
        """
        result = await self.async_call(call.UnlockConnector(connector_id=connector_id))
        return str(result.status)

    async def async_disconnect(self) -> None:
        """Close the charge point's connection. It may connect again."""
        session = self._session
        if session is not None:
            await session.ws.close()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    async def _async_serve(self, ws: web.WebSocketResponse) -> None:
        """Handle the charge point's messages until ``ws`` closes."""
        old = self._session
        handler = _OcppHandler(self, _WebSocketConnection(ws))
        session = _Session(ws, handler, self._settings.call_timeout)
        self._session = session
        if old is not None:
            # The charge point reconnected before its old connection timed
            # out. Closing waits for a close frame the old peer may never
            # send, so it runs in the background.
            _LOGGER.debug("Charge point %s replaced its connection", self._id)
            old.mark_closed()
            task = asyncio.create_task(old.ws.close())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        self._last_seen = datetime.now(UTC)
        _LOGGER.debug("Charge point %s connected", self._id)
        self._emit(ConnectedEvent(self._id))
        try:
            await handler.start()
        except (_ConnectionClosed, ConnectionError):
            pass
        finally:
            session.mark_closed()
            if self._session is session:
                self._session = None
                _LOGGER.debug("Charge point %s disconnected", self._id)
                self._emit(DisconnectedEvent(self._id))
            await ws.close()

    def _emit(self, event: ChargePointEvent) -> None:
        """Hand ``event`` to this charge point's listeners, then the central
        system's."""
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-exception-caught
                _LOGGER.exception("Listener failed on %s", type(event).__name__)
        self._settings.emit(event)

    def _seen(self) -> None:
        self._last_seen = datetime.now(UTC)

    async def _on_boot(self, info: BootInfo) -> tuple[RegistrationStatus, int]:
        self._seen()
        hook = self._settings.registration
        status = (
            await hook(self, info) if hook is not None else RegistrationStatus.ACCEPTED
        )
        self._boot = info
        self._registration_status = status
        self._emit(BootEvent(self._id, info))
        interval = (
            self._settings.heartbeat_interval
            if status is RegistrationStatus.ACCEPTED
            else DEFAULT_BOOT_RETRY_INTERVAL
        )
        return status, interval

    async def _authorize(self, id_tag: str) -> AuthorizationStatus:
        self._seen()
        hook = self._settings.authorize
        if hook is None:
            return AuthorizationStatus.ACCEPTED
        return await hook(self, id_tag)

    def _on_status(self, connector: ConnectorState) -> None:
        self._seen()
        self._connectors[connector.connector_id] = connector
        self._emit(StatusEvent(self._id, connector))

    def _on_meter_values(
        self,
        connector_id: int,
        transaction_id: int | None,
        readings: tuple[MeterReading, ...],
    ) -> None:
        self._seen()
        latest = self._meter.setdefault(connector_id, {})
        for reading in readings:
            latest[(reading.measurand, reading.phase, reading.location)] = reading
        self._emit(MeterValuesEvent(self._id, connector_id, transaction_id, readings))

    def _on_start_transaction(self, transaction: Transaction) -> None:
        self._transactions[transaction.transaction_id] = transaction
        self._emit(TransactionStartedEvent(self._id, transaction))

    def _on_stop_transaction(
        self,
        transaction_id: int,
        meter_stop: int,
        stopped_at: datetime | None,
        reason: str | None,
        readings: tuple[MeterReading, ...],
    ) -> None:
        self._seen()
        running = self._transactions.pop(transaction_id, None)
        if readings and running is not None:
            self._on_meter_values(running.connector_id, transaction_id, readings)
        if running is None:
            # Started before this central system was running.
            _LOGGER.debug(
                "Charge point %s stopped unknown transaction %s",
                self._id,
                transaction_id,
            )
            return
        stopped = replace(
            running, meter_stop=meter_stop, stopped_at=stopped_at, stop_reason=reason
        )
        self._emit(TransactionStoppedEvent(self._id, stopped))

    async def _on_data_transfer(
        self, vendor_id: str, message_id: str | None, data: str | None
    ) -> tuple[DataTransferStatus, str | None]:
        self._seen()
        handler = self._settings.data_transfer_handlers.get(vendor_id)
        if handler is None:
            _LOGGER.debug(
                "Charge point %s sent DataTransfer for unhandled vendor %s",
                self._id,
                vendor_id,
            )
            return DataTransferStatus.UNKNOWN_VENDOR_ID, None
        status, reply = await handler(self, message_id, data)
        # Raises for a status OCPP does not define, which the charge point
        # receives as an InternalError.
        return DataTransferStatus(status), reply

    def _on_firmware_status(self, status: str) -> None:
        self._seen()
        self._firmware_status = status
        self._emit(FirmwareStatusEvent(self._id, status))

    def _on_diagnostics_status(self, status: str) -> None:
        self._seen()
        self._diagnostics_status = status
        self._emit(DiagnosticsStatusEvent(self._id, status))

    def _on_security_event(
        self, event_type: str, timestamp: datetime | None, tech_info: str | None
    ) -> None:
        self._seen()
        self._emit(SecurityEvent(self._id, event_type, timestamp, tech_info))


class _OcppHandler(OcppChargePoint):  # type: ignore[misc]
    """Answers a charge point's OCPP requests on behalf of a
    :class:`ChargePoint`."""

    def __init__(self, owner: ChargePoint, connection: _WebSocketConnection) -> None:
        super().__init__(
            owner.id,
            connection,
            response_timeout=owner._settings.call_timeout,
            logger=_WireLogger(_WIRE_LOGGER, {"charge_point_id": owner.id}),
        )
        self._owner = owner

    @_on(Action.boot_notification)
    async def on_boot_notification(
        self, charge_point_vendor: str, charge_point_model: str, **kwargs: Any
    ) -> call_result.BootNotification:
        """Register the charge point and say how often to heartbeat."""
        info = BootInfo(
            vendor=charge_point_vendor,
            model=charge_point_model,
            serial_number=kwargs.get("charge_point_serial_number"),
            charge_box_serial_number=kwargs.get("charge_box_serial_number"),
            firmware_version=kwargs.get("firmware_version"),
            iccid=kwargs.get("iccid"),
            imsi=kwargs.get("imsi"),
            meter_type=kwargs.get("meter_type"),
            meter_serial_number=kwargs.get("meter_serial_number"),
        )
        status, interval = await self._owner._on_boot(info)
        return call_result.BootNotification(
            current_time=_now(), interval=interval, status=status.value
        )

    @_on(Action.heartbeat)
    def on_heartbeat(self) -> call_result.Heartbeat:
        """Answer with the current time."""
        self._owner._seen()
        return call_result.Heartbeat(current_time=_now())

    @_on(Action.status_notification)
    def on_status_notification(
        self, connector_id: int, error_code: str, status: str, **kwargs: Any
    ) -> call_result.StatusNotification:
        """Record a connector status."""
        self._owner._on_status(
            ConnectorState(
                connector_id=connector_id,
                status=ConnectorStatus(status),
                error_code=ChargePointErrorCode(error_code),
                info=kwargs.get("info"),
                vendor_id=kwargs.get("vendor_id"),
                vendor_error_code=kwargs.get("vendor_error_code"),
                timestamp=_parse_time(kwargs.get("timestamp")),
            )
        )
        return call_result.StatusNotification()

    @_on(Action.meter_values)
    def on_meter_values(
        self,
        connector_id: int,
        meter_value: list[dict[str, Any]],
        transaction_id: int | None = None,
    ) -> call_result.MeterValues:
        """Record meter values."""
        # Some charge points send 0 for "no transaction"; no id given out is 0.
        self._owner._on_meter_values(
            connector_id, transaction_id or None, _parse_readings(meter_value)
        )
        return call_result.MeterValues()

    @_on(Action.authorize)
    async def on_authorize(self, id_tag: str) -> call_result.Authorize:
        """Say whether an id tag may charge."""
        status = await self._owner._authorize(id_tag)
        return call_result.Authorize(id_tag_info=IdTagInfo(status=status.value))

    @_on(Action.start_transaction)
    async def on_start_transaction(
        self,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str,
        reservation_id: int | None = None,
    ) -> call_result.StartTransaction:
        """Record a new session and give it an id."""
        status = await self._owner._authorize(id_tag)
        transaction_id = next(self._owner._settings.transaction_ids)
        self._owner._on_start_transaction(
            Transaction(
                transaction_id=transaction_id,
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=meter_start,
                started_at=_parse_time(timestamp),
                reservation_id=reservation_id,
                id_tag_status=status,
            )
        )
        return call_result.StartTransaction(
            transaction_id=transaction_id, id_tag_info=IdTagInfo(status=status.value)
        )

    @_on(Action.stop_transaction)
    def on_stop_transaction(
        self,
        meter_stop: int,
        timestamp: str,
        transaction_id: int,
        reason: str | None = None,
        transaction_data: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> call_result.StopTransaction:
        """Record the end of a session."""
        self._owner._on_stop_transaction(
            transaction_id,
            meter_stop,
            _parse_time(timestamp),
            reason,
            _parse_readings(transaction_data or ()),
        )
        return call_result.StopTransaction()

    @_on(Action.data_transfer)
    async def on_data_transfer(
        self, vendor_id: str, message_id: str | None = None, data: str | None = None
    ) -> call_result.DataTransfer:
        """Pass a vendor request to its handler."""
        status, reply = await self._owner._on_data_transfer(vendor_id, message_id, data)
        return call_result.DataTransfer(status=status.value, data=reply)

    @_on(Action.firmware_status_notification)
    def on_firmware_status_notification(
        self, status: str
    ) -> call_result.FirmwareStatusNotification:
        """Record firmware update progress."""
        self._owner._on_firmware_status(status)
        return call_result.FirmwareStatusNotification()

    @_on(Action.diagnostics_status_notification)
    def on_diagnostics_status_notification(
        self, status: str
    ) -> call_result.DiagnosticsStatusNotification:
        """Record diagnostics upload progress."""
        self._owner._on_diagnostics_status(status)
        return call_result.DiagnosticsStatusNotification()

    @_on(Action.security_event_notification)
    def on_security_event_notification(
        self,
        type: str,  # pylint: disable=redefined-builtin
        timestamp: str,
        tech_info: str | None = None,
    ) -> call_result.SecurityEventNotification:
        """Report a security event."""
        self._owner._on_security_event(type, _parse_time(timestamp), tech_info)
        return call_result.SecurityEventNotification()
