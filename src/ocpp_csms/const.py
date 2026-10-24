"""Constants and enumerations used by :mod:`ocpp_csms`."""

from __future__ import annotations

from enum import StrEnum

DEFAULT_PORT = 9321
"""TCP port the central system listens on unless told otherwise."""

DEFAULT_HEARTBEAT_INTERVAL = 300
"""Seconds between ``Heartbeat`` messages that an accepted charge point is
told to keep."""

DEFAULT_BOOT_RETRY_INTERVAL = 60
"""Seconds a charge point told ``Pending`` or ``Rejected`` waits before it
sends its next ``BootNotification``."""

DEFAULT_CALL_TIMEOUT = 30.0
"""Seconds to wait for a charge point to answer a request."""

DEFAULT_PING_INTERVAL = 60.0
"""Seconds between WebSocket pings. A charge point that misses a pong is
treated as disconnected."""

SUBPROTOCOL = "ocpp1.6"
"""WebSocket subprotocol of OCPP 1.6J."""


class ConnectorStatus(StrEnum):
    """Status of a connector, or of the whole charge point for connector 0."""

    AVAILABLE = "Available"
    """Ready for a new user."""
    PREPARING = "Preparing"
    """A user is starting a session: plugged in, or authorised, but not yet
    charging."""
    CHARGING = "Charging"
    """Energy is flowing to the vehicle."""
    SUSPENDED_EVSE = "SuspendedEVSE"
    """The charge point is withholding energy, for example under a charging
    limit."""
    SUSPENDED_EV = "SuspendedEV"
    """The vehicle is not drawing energy, for example because it is full."""
    FINISHING = "Finishing"
    """The session has stopped but the connector is not free yet, for example
    because the cable is still plugged in."""
    RESERVED = "Reserved"
    """Reserved for one user."""
    UNAVAILABLE = "Unavailable"
    """Taken out of service, for example by
    :meth:`~ocpp_csms.ChargePoint.async_change_availability`."""
    FAULTED = "Faulted"
    """A fault stops the connector from charging. The error code says which."""


class ChargePointErrorCode(StrEnum):
    """Error reported alongside a connector status."""

    CONNECTOR_LOCK_FAILURE = "ConnectorLockFailure"
    """The connector failed to lock or unlock."""
    EV_COMMUNICATION_ERROR = "EVCommunicationError"
    """Communication with the vehicle failed."""
    GROUND_FAILURE = "GroundFailure"
    """A ground fault circuit interrupter tripped."""
    HIGH_TEMPERATURE = "HighTemperature"
    """The charge point is too hot."""
    INTERNAL_ERROR = "InternalError"
    """A fault in the charge point's hardware or software."""
    LOCAL_LIST_CONFLICT = "LocalListConflict"
    """The local authorisation list disagrees with the central system."""
    NO_ERROR = "NoError"
    """No error."""
    OTHER_ERROR = "OtherError"
    """An error not covered by the other codes. The vendor error code may say
    more."""
    OVER_CURRENT_FAILURE = "OverCurrentFailure"
    """The vehicle drew more current than allowed."""
    OVER_VOLTAGE = "OverVoltage"
    """Supply voltage is too high."""
    POWER_METER_FAILURE = "PowerMeterFailure"
    """The energy meter cannot be read."""
    POWER_SWITCH_FAILURE = "PowerSwitchFailure"
    """The power contactor failed to switch."""
    READER_FAILURE = "ReaderFailure"
    """The card reader failed."""
    RESET_FAILURE = "ResetFailure"
    """A reset did not complete."""
    UNDER_VOLTAGE = "UnderVoltage"
    """Supply voltage is too low."""
    WEAK_SIGNAL = "WeakSignal"
    """The charge point's network signal is weak."""


class RegistrationStatus(StrEnum):
    """Answer to a charge point's ``BootNotification``."""

    ACCEPTED = "Accepted"
    """The charge point is registered and may send any message."""
    PENDING = "Pending"
    """Not registered yet. The charge point boots again after
    :data:`DEFAULT_BOOT_RETRY_INTERVAL` and meanwhile accepts requests from
    the central system, such as configuration changes."""
    REJECTED = "Rejected"
    """Not registered. The charge point boots again after
    :data:`DEFAULT_BOOT_RETRY_INTERVAL` and sends nothing else meanwhile."""


class AuthorizationStatus(StrEnum):
    """Whether an id tag may charge."""

    ACCEPTED = "Accepted"
    """The id tag may charge."""
    BLOCKED = "Blocked"
    """The id tag is blocked."""
    EXPIRED = "Expired"
    """The id tag has expired."""
    INVALID = "Invalid"
    """The id tag is unknown."""
    CONCURRENT_TX = "ConcurrentTx"
    """The id tag is already in use by another transaction."""


class DataTransferStatus(StrEnum):
    """Answer to a ``DataTransfer``."""

    ACCEPTED = "Accepted"
    """The request was understood and acted on."""
    REJECTED = "Rejected"
    """The request was understood and refused."""
    UNKNOWN_MESSAGE_ID = "UnknownMessageId"
    """The vendor is known, the message id is not."""
    UNKNOWN_VENDOR_ID = "UnknownVendorId"
    """The vendor id is not known."""


class ResetType(StrEnum):
    """Kind of reset to request."""

    HARD = "Hard"
    """Restart the charge point's hardware. Running transactions end
    abruptly."""
    SOFT = "Soft"
    """Stop running transactions, then restart the charge point's
    software."""


class AvailabilityType(StrEnum):
    """Availability to set on a connector."""

    INOPERATIVE = "Inoperative"
    """Take the connector out of service."""
    OPERATIVE = "Operative"
    """Return the connector to service."""


class MessageTrigger(StrEnum):
    """Message a charge point can be asked to send at once."""

    BOOT_NOTIFICATION = "BootNotification"
    """Send a ``BootNotification``."""
    DIAGNOSTICS_STATUS_NOTIFICATION = "DiagnosticsStatusNotification"
    """Report the status of a diagnostics upload."""
    FIRMWARE_STATUS_NOTIFICATION = "FirmwareStatusNotification"
    """Report the status of a firmware update."""
    HEARTBEAT = "Heartbeat"
    """Send a ``Heartbeat``."""
    METER_VALUES = "MeterValues"
    """Send the current meter values."""
    STATUS_NOTIFICATION = "StatusNotification"
    """Send the status of the connector, or of every connector."""
