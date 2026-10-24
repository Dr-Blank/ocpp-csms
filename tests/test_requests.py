"""Requests the central system sends to a charge point."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from ocpp.v16 import call

from ocpp_csms import (
    AvailabilityType,
    CentralSystem,
    ChargePoint,
    Configuration,
    ConfigurationValue,
    CsmsCallError,
    CsmsConnectionError,
    CsmsTimeoutError,
    DataTransferResult,
    DataTransferStatus,
    MessageTrigger,
    ResetType,
)

from .conftest import CHARGE_POINT_ID, CentralSystemFactory, Connect, until


@pytest.mark.parametrize(
    ("send", "action", "payload", "reply"),
    [
        pytest.param(
            lambda cp: cp.async_remote_start_transaction("TAG-1", connector_id=1),
            "RemoteStartTransaction",
            {"id_tag": "TAG-1", "connector_id": 1},
            "Rejected",
            id="remote-start",
        ),
        pytest.param(
            lambda cp: cp.async_remote_start_transaction("TAG-1"),
            "RemoteStartTransaction",
            {"id_tag": "TAG-1"},
            "Accepted",
            id="remote-start-any-connector",
        ),
        pytest.param(
            lambda cp: cp.async_remote_stop_transaction(7),
            "RemoteStopTransaction",
            {"transaction_id": 7},
            "Accepted",
            id="remote-stop",
        ),
        pytest.param(
            lambda cp: cp.async_reset(),
            "Reset",
            {"type": "Soft"},
            "Accepted",
            id="reset-soft",
        ),
        pytest.param(
            lambda cp: cp.async_reset(ResetType.HARD),
            "Reset",
            {"type": "Hard"},
            "Rejected",
            id="reset-hard",
        ),
        pytest.param(
            lambda cp: cp.async_change_availability(0, AvailabilityType.INOPERATIVE),
            "ChangeAvailability",
            {"connector_id": 0, "type": "Inoperative"},
            "Scheduled",
            id="change-availability",
        ),
        pytest.param(
            lambda cp: cp.async_change_configuration("HeartbeatInterval", "60"),
            "ChangeConfiguration",
            {"key": "HeartbeatInterval", "value": "60"},
            "RebootRequired",
            id="change-configuration",
        ),
        pytest.param(
            lambda cp: cp.async_trigger_message(
                MessageTrigger.STATUS_NOTIFICATION, connector_id=1
            ),
            "TriggerMessage",
            {"requested_message": "StatusNotification", "connector_id": 1},
            "Accepted",
            id="trigger-message",
        ),
        pytest.param(
            lambda cp: cp.async_unlock_connector(1),
            "UnlockConnector",
            {"connector_id": 1},
            "Unlocked",
            id="unlock-connector",
        ),
    ],
)
async def test_request_returns_status(
    central_system: CentralSystem,
    connect: Connect,
    send: Callable[[ChargePoint], Awaitable[str]],
    action: str,
    payload: dict[str, Any],
    reply: str,
) -> None:
    fake = await connect(central_system)
    fake.replies[action] = reply

    status = await send(central_system.charge_points[CHARGE_POINT_ID])

    assert status == reply
    assert fake.requests == [(action, payload)]


async def test_data_transfer_result_unpacks(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)
    fake.data_transfer_data = "10_AC_84_00"
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    result = await charge_point.async_data_transfer("ExampleVendor", "CONFIG", "10ac84")
    status, data = result

    assert result == DataTransferResult(DataTransferStatus.ACCEPTED, "10_AC_84_00")
    assert (status, data) == ("Accepted", "10_AC_84_00")
    assert fake.requests == [
        (
            "DataTransfer",
            {"vendor_id": "ExampleVendor", "message_id": "CONFIG", "data": "10ac84"},
        )
    ]


async def test_get_configuration(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)
    fake.configuration = [
        {"key": "HeartbeatInterval", "readonly": False, "value": "300"},
        {"key": "NumberOfConnectors", "readonly": True},
    ]
    fake.unknown_keys = ["Nonsense"]
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    configuration = await charge_point.async_get_configuration(
        ["HeartbeatInterval", "NumberOfConnectors", "Nonsense"]
    )

    assert configuration == Configuration(
        {
            "HeartbeatInterval": ConfigurationValue("HeartbeatInterval", "300", False),
            "NumberOfConnectors": ConfigurationValue("NumberOfConnectors", None, True),
        },
        ("Nonsense",),
    )
    assert fake.requests == [
        (
            "GetConfiguration",
            {"key": ["HeartbeatInterval", "NumberOfConnectors", "Nonsense"]},
        )
    ]


async def test_get_all_configuration(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)

    configuration = await central_system.charge_points[
        CHARGE_POINT_ID
    ].async_get_configuration()

    assert configuration == Configuration({})
    assert fake.requests == [("GetConfiguration", {})]


async def test_call_error_raises(
    central_system: CentralSystem, connect: Connect
) -> None:
    await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    with pytest.raises(CsmsCallError) as err:
        await charge_point.async_call(call.ClearCache())

    assert err.value.code == "NotImplemented"


async def test_request_while_disconnected_raises(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]
    await fake.close()
    await until(lambda: not charge_point.connected)

    with pytest.raises(CsmsConnectionError):
        await charge_point.async_reset()


async def test_request_timeout_raises(
    make_central_system: CentralSystemFactory, connect: Connect
) -> None:
    central_system = await make_central_system(call_timeout=0.1)
    fake = await connect(central_system)
    fake.hang = True

    with pytest.raises(CsmsTimeoutError):
        await central_system.charge_points[CHARGE_POINT_ID].async_reset()


async def test_timeout_is_a_timeout_error() -> None:
    assert issubclass(CsmsTimeoutError, TimeoutError)


async def test_disconnect_fails_request_in_flight(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system)
    fake.hang = True
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    request = asyncio.create_task(charge_point.async_reset())
    await until(lambda: bool(fake.requests))
    await charge_point.async_disconnect()

    # Fails at once rather than after the 30 second call timeout.
    async with asyncio.timeout(2):
        with pytest.raises(CsmsConnectionError):
            await request


async def test_queued_requests_share_the_timeout(
    make_central_system: CentralSystemFactory, connect: Connect
) -> None:
    central_system = await make_central_system(call_timeout=0.2)
    fake = await connect(central_system)
    fake.hang = True
    charge_point = central_system.charge_points[CHARGE_POINT_ID]
    started = time.monotonic()

    results = await asyncio.gather(
        charge_point.async_reset(),
        charge_point.async_reset(),
        charge_point.async_reset(),
        return_exceptions=True,
    )

    assert all(isinstance(result, CsmsTimeoutError) for result in results)
    # Each waits at most one timeout, queueing included.
    assert time.monotonic() - started < 0.5


async def test_send_failure_raises_connection_error(
    central_system: CentralSystem,
    connect: Connect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await connect(central_system)

    async def fail(self: object, *args: object, **kwargs: object) -> None:
        raise ConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr("ocpp_csms.charge_point._OcppHandler.call", fail)

    with pytest.raises(CsmsConnectionError):
        await central_system.charge_points[CHARGE_POINT_ID].async_reset()


async def test_replaced_connection_fails_request_in_flight(
    central_system: CentralSystem, connect: Connect
) -> None:
    old = await connect(central_system)
    old.hang = True
    charge_point = central_system.charge_points[CHARGE_POINT_ID]
    request = asyncio.create_task(charge_point.async_reset())
    await until(lambda: bool(old.requests))

    new = await connect(central_system)

    async with asyncio.timeout(2):
        with pytest.raises(CsmsConnectionError):
            await request
    assert await charge_point.async_reset() == "Accepted"
    assert new.requests == [("Reset", {"type": "Soft"})]
