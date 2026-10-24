"""The central system: the WebSocket server charge points connect to."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import ssl
from collections.abc import Awaitable, Callable
from types import MappingProxyType, TracebackType
from typing import Self
from urllib.parse import unquote

from aiohttp import hdrs, web

from .charge_point import (
    AuthorizeHook,
    ChargePoint,
    DataTransferHandler,
    EventListener,
    RegistrationHook,
    _Settings,
)
from .const import (
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_PING_INTERVAL,
    DEFAULT_PORT,
    SUBPROTOCOL,
)
from .exceptions import CsmsConnectionError, CsmsError
from .models import ChargePointEvent

_LOGGER = logging.getLogger(__package__)

_CLOSE_TIMEOUT = 5.0
"""Seconds to wait for a charge point to answer a WebSocket close."""

Authenticator = Callable[[str, str | None], Awaitable[bool]]
"""Decides whether a charge point may connect.

Called with the charge point id and the password from its HTTP Basic
credentials, or ``None`` if it sent none. Returns True to let it in."""


def _basic_credentials(header: str) -> tuple[str, str] | None:
    """Username and password from an HTTP Basic ``Authorization`` header, or
    ``None`` if it is not one."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


class CentralSystem:
    """An OCPP 1.6J central system.

    Charge points connect to ``ws://<host>:<port>/<any path>/<charge point
    id>``, or ``wss://`` with an ``ssl_context``. The last path segment,
    percent-decoded once, is the charge point id. Each id gets one
    :class:`~ocpp_csms.ChargePoint`, kept in :attr:`charge_points` across
    reconnections, and for as long as the central system lives: without an
    ``authenticate`` hook, every id that connects is kept.

    A charge point must offer the ``ocpp1.6`` WebSocket subprotocol if it
    offers any. One that offers none is let in, since many do not.

    Run it as its own server::

        async with CentralSystem(port=9321) as central_system:
            central_system.add_listener(print)
            ...

    or mount :meth:`async_handle_request` in an aiohttp application that is
    already running, and skip :meth:`async_start`.

    With no hooks, every charge point is let in and registered, and every id
    tag may charge. Pass hooks to decide otherwise:

    - ``authenticate`` checks the charge point id and password before the
      WebSocket opens (OCPP security profile 1 or 2).
    - ``registration`` answers each ``BootNotification``.
    - ``authorize`` answers ``Authorize`` and ``StartTransaction``.

    Transaction ids count up from the Unix time at which the central system
    was created, so a restart reuses no id unless transactions started faster
    than one a second on average.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int = DEFAULT_PORT,
        ssl_context: ssl.SSLContext | None = None,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        ping_interval: float | None = DEFAULT_PING_INTERVAL,
        authenticate: Authenticator | None = None,
        registration: RegistrationHook | None = None,
        authorize: AuthorizeHook | None = None,
    ) -> None:
        """Configure the central system. Nothing listens until
        :meth:`async_start`.

        :param host: Address to listen on. ``None`` listens on every
            interface.
        :param port: TCP port to listen on. 0 picks a free one; read it back
            from :attr:`port`.
        :param ssl_context: Serve ``wss://`` with this context instead of
            ``ws://``.
        :param heartbeat_interval: Seconds between heartbeats an accepted
            charge point is told to keep.
        :param call_timeout: Seconds to wait for a charge point to answer a
            request.
        :param ping_interval: Seconds between WebSocket pings. A charge point
            that misses a pong is disconnected. ``None`` sends no pings.
        """
        self._host = host
        self._port = port
        self._ssl_context = ssl_context
        self._ping_interval = ping_interval
        self._authenticate = authenticate
        self._listeners: list[EventListener] = []
        self._charge_points: dict[str, ChargePoint] = {}
        self._settings = _Settings(
            heartbeat_interval=heartbeat_interval,
            call_timeout=call_timeout,
            registration=registration,
            authorize=authorize,
            data_transfer_handlers={},
            emit=self._emit,
        )
        self._runner: web.AppRunner | None = None
        self._start_lock = asyncio.Lock()
        self._stopping = False

    async def __aenter__(self) -> Self:
        """Start the server."""
        await self.async_start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the server."""
        await self.async_stop()

    @property
    def charge_points(self) -> MappingProxyType[str, ChargePoint]:
        """Every charge point that has connected, by id."""
        return MappingProxyType(self._charge_points)

    @property
    def port(self) -> int:
        """TCP port the server listens on.

        :raises CsmsError: if the server is not running.
        """
        if self._runner is None:
            raise CsmsError("central system is not running")
        for address in self._runner.addresses:
            if isinstance(address, tuple):
                return int(address[1])
        raise CsmsError("central system has no TCP address")

    def add_listener(self, listener: EventListener) -> Callable[[], None]:
        """Call ``listener`` with each event from every charge point.

        Returns a function that removes the listener.
        """
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def set_data_transfer_handler(
        self, vendor_id: str, handler: DataTransferHandler | None
    ) -> None:
        """Answer charge points' ``DataTransfer`` requests for ``vendor_id``.

        ``None`` removes the handler. Requests for a vendor without a handler
        are answered ``UnknownVendorId``.
        """
        handlers = self._settings.data_transfer_handlers
        if handler is None:
            handlers.pop(vendor_id, None)
        else:
            handlers[vendor_id] = handler

    async def async_start(self) -> None:
        """Start listening.

        :raises CsmsConnectionError: if the address cannot be bound, for
            example because the port is in use.
        """
        async with self._start_lock:
            if self._runner is None:
                await self._async_start()

    async def _async_start(self) -> None:
        app = web.Application()
        app.router.add_get("/{path:.*}", self.async_handle_request)
        runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner, self._host, self._port, ssl_context=self._ssl_context
        )
        try:
            await site.start()
        except OSError as err:
            await runner.cleanup()
            raise CsmsConnectionError(
                f"cannot listen on port {self._port}: {err.strerror}"
            ) from err
        self._runner = runner
        self._stopping = False
        _LOGGER.debug("Central system listening on port %s", self.port)

    async def async_stop(self) -> None:
        """Disconnect every charge point and stop listening."""
        self._stopping = True
        await asyncio.gather(
            *(
                charge_point.async_disconnect()
                for charge_point in list(self._charge_points.values())
            )
        )
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    async def async_handle_request(self, request: web.Request) -> web.StreamResponse:
        """Serve one charge point connection.

        An aiohttp handler for ``GET`` requests. The response finishes when
        the charge point disconnects.
        """
        if self._stopping:
            return web.Response(status=503, text="central system is stopping")
        charge_point_id = unquote(
            request.rel_url.raw_path.rstrip("/").rpartition("/")[2]
        )
        if not charge_point_id or "/" in charge_point_id:
            return web.Response(status=404, text="no charge point id in the URL")

        offered = [
            protocol.strip()
            for header in request.headers.getall(hdrs.SEC_WEBSOCKET_PROTOCOL, ())
            for protocol in header.split(",")
        ]
        if offered and SUBPROTOCOL not in offered:
            _LOGGER.debug(
                "Refused charge point %s: no %s subprotocol",
                charge_point_id,
                SUBPROTOCOL,
            )
            return web.Response(status=400, text=f"{SUBPROTOCOL} only")

        if not await self._async_authenticate(request, charge_point_id):
            _LOGGER.debug("Refused charge point %s: not authenticated", charge_point_id)
            return web.Response(
                status=401, headers={hdrs.WWW_AUTHENTICATE: 'Basic realm="OCPP"'}
            )

        ws = web.WebSocketResponse(
            protocols=(SUBPROTOCOL,),
            heartbeat=self._ping_interval,
            timeout=_CLOSE_TIMEOUT,
        )
        await ws.prepare(request)
        charge_point = self._charge_points.get(charge_point_id)
        if charge_point is None:
            charge_point = ChargePoint(charge_point_id, self._settings)
            self._charge_points[charge_point_id] = charge_point
        await charge_point._async_serve(ws)  # pylint: disable=protected-access
        return ws

    async def _async_authenticate(
        self, request: web.Request, charge_point_id: str
    ) -> bool:
        """Check the request's credentials against the ``authenticate`` hook."""
        if self._authenticate is None:
            return True
        password: str | None = None
        header = request.headers.get(hdrs.AUTHORIZATION)
        if header:
            credentials = _basic_credentials(header)
            if credentials is None or credentials[0] != charge_point_id:
                return False
            password = credentials[1]
        return await self._authenticate(charge_point_id, password)

    def _emit(self, event: ChargePointEvent) -> None:
        """Hand ``event`` to the central system's listeners."""
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-exception-caught
                _LOGGER.exception("Listener failed on %s", type(event).__name__)
