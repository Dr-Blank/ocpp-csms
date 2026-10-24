"""asyncio OCPP 1.6J central system.

Run a :class:`CentralSystem`, point charge points at it, and work with each
one through its :class:`ChargePoint`::

    from ocpp_csms import CentralSystem, ConnectedEvent

    async with CentralSystem(port=9321) as central_system:

        def on_event(event):
            if isinstance(event, ConnectedEvent):
                charge_point = central_system.charge_points[event.charge_point_id]
                ...

        central_system.add_listener(on_event)
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .charge_point import (
    AuthorizeHook,
    ChargePoint,
    DataTransferHandler,
    EventListener,
    RegistrationHook,
)
from .const import (
    DEFAULT_BOOT_RETRY_INTERVAL,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_PING_INTERVAL,
    DEFAULT_PORT,
    SUBPROTOCOL,
    AuthorizationStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ConnectorStatus,
    DataTransferStatus,
    MessageTrigger,
    RegistrationStatus,
    ResetType,
)
from .exceptions import (
    CsmsCallError,
    CsmsConnectionError,
    CsmsError,
    CsmsTimeoutError,
)
from .models import (
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
from .server import Authenticator, CentralSystem

try:
    __version__ = version("ocpp-csms")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_BOOT_RETRY_INTERVAL",
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_PING_INTERVAL",
    "DEFAULT_PORT",
    "SUBPROTOCOL",
    "Authenticator",
    "AuthorizationStatus",
    "AuthorizeHook",
    "AvailabilityType",
    "BootEvent",
    "BootInfo",
    "CentralSystem",
    "ChargePoint",
    "ChargePointErrorCode",
    "ChargePointEvent",
    "Configuration",
    "ConfigurationValue",
    "ConnectedEvent",
    "ConnectorState",
    "ConnectorStatus",
    "CsmsCallError",
    "CsmsConnectionError",
    "CsmsError",
    "CsmsTimeoutError",
    "DataTransferHandler",
    "DataTransferResult",
    "DataTransferStatus",
    "DiagnosticsStatusEvent",
    "DisconnectedEvent",
    "EventListener",
    "FirmwareStatusEvent",
    "MessageTrigger",
    "MeterReading",
    "MeterValuesEvent",
    "RegistrationHook",
    "RegistrationStatus",
    "ResetType",
    "SecurityEvent",
    "StatusEvent",
    "Transaction",
    "TransactionStartedEvent",
    "TransactionStoppedEvent",
    "__version__",
]
