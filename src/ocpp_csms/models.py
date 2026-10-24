"""State reported by charge points, and the events that report it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from .const import (
    AuthorizationStatus,
    ChargePointErrorCode,
    ConnectorStatus,
    DataTransferStatus,
)

DEFAULT_MEASURAND = "Energy.Active.Import.Register"
"""Measurand of a sampled value that names none."""

DEFAULT_UNIT = "Wh"
"""Unit of an energy sampled value that names none."""

ENERGY_MEASURAND_PREFIX = "Energy."
"""Prefix of the measurands that default to :data:`DEFAULT_UNIT`."""

SIGNED_DATA = "SignedData"
"""Value format of a sampled value whose value is a signed blob rather than a
number."""


@dataclass(frozen=True, slots=True)
class BootInfo:
    """What a charge point says about itself in its ``BootNotification``."""

    vendor: str
    """Charge point vendor."""
    model: str
    """Charge point model."""
    serial_number: str | None = None
    """Charge point serial number."""
    charge_box_serial_number: str | None = None
    """Serial number of the charge box inside the charge point."""
    firmware_version: str | None = None
    """Firmware version."""
    iccid: str | None = None
    """ICCID of the modem's SIM card."""
    imsi: str | None = None
    """IMSI of the modem's SIM card."""
    meter_type: str | None = None
    """Type of the main energy meter."""
    meter_serial_number: str | None = None
    """Serial number of the main energy meter."""


@dataclass(frozen=True, slots=True)
class ConnectorState:
    """Last status a charge point reported for one connector.

    Connector 0 stands for the charge point as a whole.
    """

    connector_id: int
    """Connector the status is for, or 0 for the whole charge point."""
    status: ConnectorStatus
    """Status of the connector."""
    error_code: ChargePointErrorCode
    """Error, or :attr:`~ChargePointErrorCode.NO_ERROR`."""
    info: str | None = None
    """Free text about the error."""
    vendor_id: str | None = None
    """Vendor whose error code :attr:`vendor_error_code` is."""
    vendor_error_code: str | None = None
    """Vendor specific error code."""
    timestamp: datetime | None = None
    """When the charge point saw the status, if it said."""


@dataclass(frozen=True, slots=True)
class MeterReading:
    """One sampled value from a ``MeterValues``, ``StopTransaction`` or
    similar message."""

    value: str
    """The value as sent. A number for :attr:`format` ``Raw``, a signed blob
    for ``SignedData``. :attr:`numeric` gives the number."""
    measurand: str = DEFAULT_MEASURAND
    """What is measured, for example ``Power.Active.Import``."""
    unit: str | None = DEFAULT_UNIT
    """Unit of :attr:`value`, for example ``W``, ``Wh``, ``kWh``, ``A`` or
    ``V``. ``None`` when the charge point named none for a measurand other
    than energy."""
    phase: str | None = None
    """Phase measured, for example ``L1`` or ``L1-N``, or ``None`` for all
    phases."""
    location: str | None = None
    """Where it is measured, for example ``Outlet``."""
    context: str | None = None
    """Why it was sampled, for example ``Sample.Periodic`` or
    ``Transaction.End``."""
    format: str | None = None
    """``Raw`` or ``SignedData``. ``None`` means ``Raw``."""
    timestamp: datetime | None = None
    """When the value was sampled."""

    @property
    def numeric(self) -> float | None:
        """:attr:`value` as a number, or ``None`` if it is not one."""
        if self.format == SIGNED_DATA:
            return None
        try:
            return float(self.value)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class Transaction:
    """A charging session as the charge point reported it."""

    transaction_id: int
    """Id the central system gave the transaction."""
    connector_id: int
    """Connector the session runs on."""
    id_tag: str
    """Id tag that started the session."""
    meter_start: int
    """Energy meter reading in Wh when the session started."""
    started_at: datetime | None = None
    """When the session started."""
    reservation_id: int | None = None
    """Reservation the session used, if any."""
    id_tag_status: AuthorizationStatus = AuthorizationStatus.ACCEPTED
    """Answer the central system gave :attr:`id_tag` when the session
    started. For any answer but ``Accepted`` the charge point stops the
    session, or, if configured so, keeps it running without charging."""
    meter_stop: int | None = None
    """Energy meter reading in Wh when the session stopped, while it is
    running ``None``."""
    stopped_at: datetime | None = None
    """When the session stopped, while it is running ``None``."""
    stop_reason: str | None = None
    """Why the session stopped, for example ``EVDisconnected`` or ``Remote``.
    ``None`` while running, and for a session stopped by its id tag."""

    @property
    def energy_wh(self) -> int | None:
        """Energy delivered in Wh, once the session has stopped."""
        if self.meter_stop is None:
            return None
        return self.meter_stop - self.meter_start


class DataTransferResult(NamedTuple):
    """A charge point's answer to a ``DataTransfer``.

    Unpacks as ``status, data``.
    """

    status: DataTransferStatus
    """Whether the charge point acted on the request."""
    data: str | None = None
    """Data the charge point sent back, if any."""


@dataclass(frozen=True, slots=True)
class ConfigurationValue:
    """One configuration key of a charge point."""

    key: str
    """Name of the key, for example ``HeartbeatInterval``."""
    value: str | None
    """Current value, or ``None`` if the charge point did not say."""
    readonly: bool
    """True if the key cannot be changed."""


@dataclass(frozen=True, slots=True)
class Configuration:
    """A charge point's answer to ``GetConfiguration``."""

    values: dict[str, ConfigurationValue]
    """Keys the charge point knows, by name."""
    unknown_keys: tuple[str, ...] = ()
    """Requested keys the charge point does not know."""


@dataclass(frozen=True, slots=True)
class ChargePointEvent:
    """Base class of every event a charge point emits."""

    charge_point_id: str
    """Charge point the event comes from."""


@dataclass(frozen=True, slots=True)
class ConnectedEvent(ChargePointEvent):
    """The charge point opened its connection."""


@dataclass(frozen=True, slots=True)
class DisconnectedEvent(ChargePointEvent):
    """The charge point's connection closed."""


@dataclass(frozen=True, slots=True)
class BootEvent(ChargePointEvent):
    """The charge point sent a ``BootNotification``."""

    info: BootInfo
    """What the charge point said about itself."""


@dataclass(frozen=True, slots=True)
class StatusEvent(ChargePointEvent):
    """The charge point reported a connector status."""

    connector: ConnectorState
    """The reported status."""


@dataclass(frozen=True, slots=True)
class MeterValuesEvent(ChargePointEvent):
    """The charge point sent meter values."""

    connector_id: int
    """Connector the values are for, or 0 for the whole charge point."""
    transaction_id: int | None
    """Transaction the values belong to, or ``None`` outside a transaction,
    including when the charge point sends 0."""
    readings: tuple[MeterReading, ...]
    """The sampled values, oldest first."""


@dataclass(frozen=True, slots=True)
class TransactionStartedEvent(ChargePointEvent):
    """A charging session started."""

    transaction: Transaction
    """The session."""


@dataclass(frozen=True, slots=True)
class TransactionStoppedEvent(ChargePointEvent):
    """A charging session stopped."""

    transaction: Transaction
    """The session, with its stop fields filled in."""


@dataclass(frozen=True, slots=True)
class FirmwareStatusEvent(ChargePointEvent):
    """The charge point reported progress of a firmware update."""

    status: str
    """For example ``Downloading``, ``Installed`` or ``InstallationFailed``."""


@dataclass(frozen=True, slots=True)
class DiagnosticsStatusEvent(ChargePointEvent):
    """The charge point reported progress of a diagnostics upload."""

    status: str
    """For example ``Uploading``, ``Uploaded`` or ``UploadFailed``."""


@dataclass(frozen=True, slots=True)
class SecurityEvent(ChargePointEvent):
    """The charge point reported a security event."""

    type: str
    """Kind of event, for example ``FirmwareUpdated`` or
    ``InvalidCentralSystemCertificate``."""
    timestamp: datetime | None
    """When it happened."""
    tech_info: str | None = None
    """Further detail from the charge point."""
