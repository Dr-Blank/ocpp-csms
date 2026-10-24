"""Messages a charge point sends, and what the central system makes of them."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from ocpp.v16 import call

from ocpp_csms import (
    AuthorizationStatus,
    BootEvent,
    BootInfo,
    CentralSystem,
    ChargePoint,
    ChargePointErrorCode,
    ChargePointEvent,
    ConnectedEvent,
    ConnectorStatus,
    DataTransferStatus,
    DiagnosticsStatusEvent,
    FirmwareStatusEvent,
    MeterReading,
    MeterValuesEvent,
    RegistrationStatus,
    SecurityEvent,
    StatusEvent,
    TransactionStartedEvent,
    TransactionStoppedEvent,
)

from .conftest import CHARGE_POINT_ID, CentralSystemFactory, Connect, until

TIMESTAMP = "2026-01-02T03:04:05Z"
PARSED_TIMESTAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _boot() -> call.BootNotification:
    return call.BootNotification(
        charge_point_vendor="ExampleVendor",
        charge_point_model="ExampleModel",
        charge_point_serial_number="SN-0000",
        firmware_version="1.0.0",
    )


async def test_boot_is_accepted_by_default(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)

    result = await fake.call(_boot())

    assert result.status == "Accepted"
    assert result.interval == 300
    charge_point = central_system.charge_points[CHARGE_POINT_ID]
    info = BootInfo(
        vendor="ExampleVendor",
        model="ExampleModel",
        serial_number="SN-0000",
        firmware_version="1.0.0",
    )
    assert charge_point.boot == info
    assert charge_point.registration_status is RegistrationStatus.ACCEPTED
    assert events == [ConnectedEvent(CHARGE_POINT_ID), BootEvent(CHARGE_POINT_ID, info)]


@pytest.mark.parametrize(
    ("status", "interval"),
    [
        pytest.param(RegistrationStatus.ACCEPTED, 300, id="accepted"),
        pytest.param(RegistrationStatus.PENDING, 60, id="pending"),
        pytest.param(RegistrationStatus.REJECTED, 60, id="rejected"),
    ],
)
async def test_registration_hook_answers_boot(
    make_central_system: CentralSystemFactory,
    connect: Connect,
    status: RegistrationStatus,
    interval: int,
) -> None:
    seen: list[tuple[str, BootInfo]] = []

    async def registration(
        charge_point: ChargePoint, info: BootInfo
    ) -> RegistrationStatus:
        seen.append((charge_point.id, info))
        return status

    central_system = await make_central_system(registration=registration)
    fake = await connect(central_system)

    result = await fake.call(_boot())

    assert (result.status, result.interval) == (status.value, interval)
    assert seen == [
        (
            CHARGE_POINT_ID,
            BootInfo(
                "ExampleVendor", "ExampleModel", "SN-0000", firmware_version="1.0.0"
            ),
        )
    ]
    assert central_system.charge_points[CHARGE_POINT_ID].registration_status is status


async def test_heartbeat_interval_is_configurable(
    make_central_system: CentralSystemFactory, connect: Connect
) -> None:
    central_system = await make_central_system(heartbeat_interval=42)
    fake = await connect(central_system)

    result = await fake.call(_boot())

    assert result.interval == 42


async def test_heartbeat_answers_with_time(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]
    before = charge_point.last_seen

    result = await fake.call(call.Heartbeat())

    assert datetime.fromisoformat(result.current_time).tzinfo is not None
    assert charge_point.last_seen is not None
    assert before is not None
    assert charge_point.last_seen >= before


async def test_status_notification_is_recorded(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)

    await fake.call(
        call.StatusNotification(
            connector_id=1,
            error_code="OverCurrentFailure",
            status="Faulted",
            timestamp=TIMESTAMP,
            vendor_error_code="E42",
        )
    )

    connector = central_system.charge_points[CHARGE_POINT_ID].connectors[1]
    assert connector.status is ConnectorStatus.FAULTED
    assert connector.error_code is ChargePointErrorCode.OVER_CURRENT_FAILURE
    assert connector.vendor_error_code == "E42"
    assert connector.timestamp == PARSED_TIMESTAMP
    assert events[-1] == StatusEvent(CHARGE_POINT_ID, connector)


async def test_meter_values_keep_latest_reading(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)

    await fake.call(
        call.MeterValues(
            connector_id=1,
            transaction_id=7,
            meter_value=[
                {
                    "timestamp": TIMESTAMP,
                    "sampledValue": [
                        {"value": "1000"},
                        {
                            "value": "3600",
                            "measurand": "Power.Active.Import",
                            "unit": "W",
                        },
                        {
                            "value": "16.1",
                            "measurand": "Current.Import",
                            "unit": "A",
                            "phase": "L1",
                        },
                        {"value": "230", "measurand": "Voltage"},
                    ],
                },
                {"timestamp": TIMESTAMP, "sampledValue": [{"value": "1500"}]},
            ],
        )
    )

    charge_point = central_system.charge_points[CHARGE_POINT_ID]
    energy = charge_point.get_meter_reading(1)
    assert energy == MeterReading(value="1500", timestamp=PARSED_TIMESTAMP)
    assert energy.numeric == 1500.0
    power = charge_point.get_meter_reading(1, "Power.Active.Import")
    assert power is not None
    assert (power.numeric, power.unit) == (3600.0, "W")
    current = charge_point.get_meter_reading(1, "Current.Import", phase="L1")
    assert current is not None
    assert current.numeric == 16.1
    assert charge_point.get_meter_reading(1, "Current.Import") is None
    voltage = charge_point.get_meter_reading(1, "Voltage")
    assert voltage is not None
    # Only energy measurands default to Wh.
    assert voltage.unit is None
    assert charge_point.get_meter_reading(2) is None
    assert len(charge_point.meter_readings(1)) == 4
    event = events[-1]
    assert isinstance(event, MeterValuesEvent)
    assert (event.connector_id, event.transaction_id, len(event.readings)) == (1, 7, 5)


@pytest.mark.parametrize(
    "transaction_id",
    [pytest.param(None, id="omitted"), pytest.param(0, id="zero")],
)
async def test_meter_values_outside_a_transaction(
    central_system: CentralSystem,
    events: list[ChargePointEvent],
    connect: Connect,
    transaction_id: int | None,
) -> None:
    fake = await connect(central_system)

    await fake.call(
        call.MeterValues(
            connector_id=1,
            transaction_id=transaction_id,
            meter_value=[{"timestamp": TIMESTAMP, "sampledValue": [{"value": "1"}]}],
        )
    )

    event = events[-1]
    assert isinstance(event, MeterValuesEvent)
    assert event.transaction_id is None


@pytest.mark.parametrize(
    ("value", "value_format", "numeric"),
    [
        pytest.param("12.5", None, 12.5, id="raw"),
        pytest.param("12.5", "Raw", 12.5, id="explicit-raw"),
        pytest.param("not a number", None, None, id="not-numeric"),
        pytest.param("12.5", "SignedData", None, id="signed"),
    ],
)
def test_meter_reading_numeric(
    value: str, value_format: str | None, numeric: float | None
) -> None:
    assert MeterReading(value=value, format=value_format).numeric == numeric


async def test_transaction_start_and_stop(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    started = await fake.call(
        call.StartTransaction(
            connector_id=1, id_tag="TAG-1", meter_start=1000, timestamp=TIMESTAMP
        )
    )

    assert started.id_tag_info["status"] == "Accepted"
    transaction = charge_point.get_transaction(1)
    assert transaction is not None
    assert charge_point.transactions == {started.transaction_id: transaction}
    assert transaction.transaction_id == started.transaction_id
    assert (transaction.id_tag, transaction.meter_start) == ("TAG-1", 1000)
    assert transaction.started_at == PARSED_TIMESTAMP
    assert transaction.energy_wh is None
    assert events[-1] == TransactionStartedEvent(CHARGE_POINT_ID, transaction)

    await fake.call(
        call.StopTransaction(
            meter_stop=4500,
            timestamp=TIMESTAMP,
            transaction_id=started.transaction_id,
            reason="EVDisconnected",
            transaction_data=[
                {
                    "timestamp": TIMESTAMP,
                    "sampledValue": [{"value": "4500", "context": "Transaction.End"}],
                }
            ],
        )
    )

    assert charge_point.get_transaction(1) is None
    assert not charge_point.transactions
    stopped = events[-1]
    assert isinstance(stopped, TransactionStoppedEvent)
    assert stopped.transaction.energy_wh == 3500
    assert stopped.transaction.stop_reason == "EVDisconnected"
    assert stopped.transaction.stopped_at == PARSED_TIMESTAMP
    energy = charge_point.get_meter_reading(1)
    assert energy is not None
    assert energy.context == "Transaction.End"


async def test_transaction_ids_are_unique(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)
    start = call.StartTransaction(
        connector_id=1, id_tag="TAG-1", meter_start=0, timestamp=TIMESTAMP
    )

    first = await fake.call(start)
    second = await fake.call(start)

    assert first.transaction_id >= int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    assert second.transaction_id == first.transaction_id + 1


async def test_second_start_on_a_connector_keeps_the_first(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]
    start = call.StartTransaction(
        connector_id=1, id_tag="TAG-1", meter_start=0, timestamp=TIMESTAMP
    )
    first = await fake.call(start)
    second = await fake.call(start)

    latest = charge_point.get_transaction(1)
    assert latest is not None
    assert latest.transaction_id == second.transaction_id

    await fake.call(
        call.StopTransaction(
            meter_stop=10, timestamp=TIMESTAMP, transaction_id=first.transaction_id
        )
    )

    stopped = events[-1]
    assert isinstance(stopped, TransactionStoppedEvent)
    assert stopped.transaction.transaction_id == first.transaction_id
    assert list(charge_point.transactions) == [second.transaction_id]


async def test_stop_of_unknown_transaction_is_acknowledged(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)

    await fake.call(
        call.StopTransaction(meter_stop=10, timestamp=TIMESTAMP, transaction_id=99)
    )

    assert not any(isinstance(event, TransactionStoppedEvent) for event in events)


async def test_authorize_hook_answers_authorize_and_start(
    make_central_system: CentralSystemFactory, connect: Connect
) -> None:
    async def authorize(charge_point: ChargePoint, id_tag: str) -> AuthorizationStatus:
        return (
            AuthorizationStatus.ACCEPTED
            if id_tag == "GOOD"
            else AuthorizationStatus.BLOCKED
        )

    central_system = await make_central_system(authorize=authorize)
    fake = await connect(central_system)

    good = await fake.call(call.Authorize(id_tag="GOOD"))
    bad = await fake.call(call.Authorize(id_tag="BAD"))
    start = await fake.call(
        call.StartTransaction(
            connector_id=1, id_tag="BAD", meter_start=0, timestamp=TIMESTAMP
        )
    )

    assert good.id_tag_info["status"] == "Accepted"
    assert bad.id_tag_info["status"] == "Blocked"
    assert start.id_tag_info["status"] == "Blocked"
    # The charge point decides what happens to the session, so it is tracked.
    transaction = central_system.charge_points[CHARGE_POINT_ID].get_transaction(1)
    assert transaction is not None
    assert transaction.id_tag_status is AuthorizationStatus.BLOCKED


async def test_authorize_accepts_by_default(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)

    result = await fake.call(call.Authorize(id_tag="ANY"))

    assert result.id_tag_info["status"] == "Accepted"


async def test_data_transfer_without_handler_is_unknown_vendor(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)

    result = await fake.call(call.DataTransfer(vendor_id="Nobody", data="x"))

    assert result.status == "UnknownVendorId"


async def test_data_transfer_goes_to_vendor_handler(
    central_system: CentralSystem, connect: Connect
) -> None:
    seen: list[tuple[str, str | None, str | None]] = []

    async def handler(
        charge_point: ChargePoint, message_id: str | None, data: str | None
    ) -> tuple[DataTransferStatus, str | None]:
        seen.append((charge_point.id, message_id, data))
        return DataTransferStatus.ACCEPTED, "pong"

    central_system.set_data_transfer_handler("ExampleVendor", handler)
    fake = await connect(central_system)

    result = await fake.call(
        call.DataTransfer(vendor_id="ExampleVendor", message_id="ping", data="1")
    )

    assert (result.status, result.data) == ("Accepted", "pong")
    assert seen == [(CHARGE_POINT_ID, "ping", "1")]

    central_system.set_data_transfer_handler("ExampleVendor", None)
    result = await fake.call(call.DataTransfer(vendor_id="ExampleVendor"))
    assert result.status == "UnknownVendorId"


async def test_data_transfer_handler_with_invalid_status_fails_the_request(
    central_system: CentralSystem, connect: Connect
) -> None:
    async def handler(
        charge_point: ChargePoint, message_id: str | None, data: str | None
    ) -> tuple[DataTransferStatus, str | None]:
        return "Maybe", None  # type: ignore[return-value]

    central_system.set_data_transfer_handler("ExampleVendor", handler)
    fake = await connect(central_system)

    # The fake answers a CallError with None.
    result = await fake.call(call.DataTransfer(vendor_id="ExampleVendor"))

    assert result is None


async def test_progress_and_security_notifications(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    await fake.call(call.FirmwareStatusNotification(status="Installed"))
    await fake.call(call.DiagnosticsStatusNotification(status="Uploaded"))
    await fake.call(
        call.SecurityEventNotification(
            type="FirmwareUpdated", timestamp=TIMESTAMP, tech_info="1.0.1"
        )
    )

    assert charge_point.firmware_status == "Installed"
    assert charge_point.diagnostics_status == "Uploaded"
    assert events[-3:] == [
        FirmwareStatusEvent(CHARGE_POINT_ID, "Installed"),
        DiagnosticsStatusEvent(CHARGE_POINT_ID, "Uploaded"),
        SecurityEvent(CHARGE_POINT_ID, "FirmwareUpdated", PARSED_TIMESTAMP, "1.0.1"),
    ]


async def test_failing_listener_does_not_break_handling(
    central_system: CentralSystem,
    connect: Connect,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(event: ChargePointEvent) -> None:
        raise RuntimeError("listener bug")

    central_system.add_listener(broken)
    fake = await connect(central_system)
    central_system.charge_points[CHARGE_POINT_ID].add_listener(broken)

    result = await fake.call(_boot())

    assert result.status == "Accepted"
    assert "Listener failed on BootEvent" in caplog.text


async def test_removed_listener_gets_nothing(
    central_system: CentralSystem, connect: Connect
) -> None:
    hub: list[ChargePointEvent] = []
    own: list[ChargePointEvent] = []
    remove_hub = central_system.add_listener(hub.append)
    fake = await connect(central_system)
    remove_own = central_system.charge_points[CHARGE_POINT_ID].add_listener(own.append)
    remove_hub()
    remove_own()
    remove_own()

    await fake.call(_boot())

    assert hub == [ConnectedEvent(CHARGE_POINT_ID)]
    assert own == []


async def test_payloads_stay_out_of_info_logs(
    central_system: CentralSystem,
    connect: Connect,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    fake = await connect(central_system)

    await fake.call(call.DataTransfer(vendor_id="Nobody", data="secret-value"))

    assert "secret-value" not in "".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("ocpp_csms")
    )


@pytest.mark.parametrize(
    ("logger", "logged"),
    [
        pytest.param("ocpp_csms", False, id="package-debug"),
        pytest.param("ocpp_csms.wire", True, id="wire-debug"),
    ],
)
async def test_wire_logging_must_be_asked_for_by_name(
    central_system: CentralSystem,
    connect: Connect,
    caplog: pytest.LogCaptureFixture,
    logger: str,
    logged: bool,
) -> None:
    caplog.set_level(logging.DEBUG, logger=logger)
    fake = await connect(central_system)

    await fake.call(call.DataTransfer(vendor_id="Nobody", data="secret-value"))

    assert ("secret-value" in caplog.text) is logged


async def test_payloads_stay_out_of_error_logs(
    central_system: CentralSystem,
    connect: Connect,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    fake = await connect(central_system)

    # Breaks the schema, so the ocpp package logs the request as an error.
    await fake.connection.ws.send_str(
        '[2,"9","DataTransfer",{"vendorId":"Nobody","data":"secret-value","x":1}]'
    )
    await until(
        lambda: any(record.levelno >= logging.WARNING for record in caplog.records)
    )

    library = [r for r in caplog.records if r.name.startswith("ocpp_csms")]
    assert "secret-value" not in "".join(r.getMessage() for r in library)
    assert any(
        f"charge point {CHARGE_POINT_ID} failed" in r.getMessage() for r in library
    )
