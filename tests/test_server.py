"""Connections: who gets in, reconnection, and running the server."""

from __future__ import annotations

import base64
import socket
from collections.abc import Iterator

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from ocpp.v16 import call

from ocpp_csms import (
    CentralSystem,
    ChargePointEvent,
    ConnectedEvent,
    CsmsConnectionError,
    CsmsError,
    DisconnectedEvent,
)
from ocpp_csms.__main__ import main

from .conftest import (
    CHARGE_POINT_ID,
    CentralSystemFactory,
    ClientConnection,
    Connect,
    FakeChargePoint,
    until,
)

PASSWORD = "example-password"


def _basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def _authenticate(charge_point_id: str, password: str | None) -> bool:
    return (charge_point_id, password) == (CHARGE_POINT_ID, PASSWORD)


async def test_disconnect_and_reconnect_keep_the_charge_point(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    fake = await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    await fake.close()
    await until(lambda: not charge_point.connected)
    await connect(central_system)

    assert central_system.charge_points[CHARGE_POINT_ID] is charge_point
    assert charge_point.connected
    assert events == [
        ConnectedEvent(CHARGE_POINT_ID),
        DisconnectedEvent(CHARGE_POINT_ID),
        ConnectedEvent(CHARGE_POINT_ID),
    ]


async def test_new_connection_replaces_old_one(
    central_system: CentralSystem, events: list[ChargePointEvent], connect: Connect
) -> None:
    old = await connect(central_system)
    new = await connect(central_system)
    charge_point = central_system.charge_points[CHARGE_POINT_ID]

    await until(lambda: old.connection.ws.closed)
    result = await new.call(call.Heartbeat())

    assert result.current_time
    assert charge_point.connected
    assert DisconnectedEvent(CHARGE_POINT_ID) not in events


async def test_charge_points_are_kept_apart(
    central_system: CentralSystem, connect: Connect
) -> None:
    first = await connect(central_system, "CP-A")
    await connect(central_system, "CP-B")

    await first.call(
        call.StatusNotification(connector_id=1, error_code="NoError", status="Charging")
    )

    assert set(central_system.charge_points) == {"CP-A", "CP-B"}
    assert 1 in central_system.charge_points["CP-A"].connectors
    assert not central_system.charge_points["CP-B"].connectors


@pytest.mark.parametrize(
    ("path", "charge_point_id"),
    [
        pytest.param("CP%20001", "CP 001", id="space"),
        pytest.param("CP%2541", "CP%41", id="decoded-once"),
        pytest.param("CP001/", "CP001", id="trailing-slash"),
    ],
)
async def test_charge_point_id_is_url_decoded(
    central_system: CentralSystem,
    session: aiohttp.ClientSession,
    path: str,
    charge_point_id: str,
) -> None:
    async with session.ws_connect(
        f"ws://127.0.0.1:{central_system.port}/ocpp/{path}", protocols=("ocpp1.6",)
    ):
        await until(lambda: charge_point_id in central_system.charge_points)


@pytest.mark.parametrize(
    "path", [pytest.param("", id="empty"), pytest.param("ocpp/A%2FB", id="slash")]
)
async def test_bad_charge_point_id_is_refused(
    central_system: CentralSystem, session: aiohttp.ClientSession, path: str
) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await session.ws_connect(
            f"ws://127.0.0.1:{central_system.port}/{path}", protocols=("ocpp1.6",)
        )

    assert err.value.status == 404


async def test_other_subprotocol_is_refused(
    central_system: CentralSystem, connect: Connect
) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await connect(central_system, protocols=("ocpp2.0.1",))

    assert err.value.status == 400


async def test_missing_subprotocol_is_let_in(
    central_system: CentralSystem, connect: Connect
) -> None:
    fake = await connect(central_system, protocols=())

    result = await fake.call(call.Heartbeat())

    assert result.current_time


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-credentials"),
        pytest.param(_basic(CHARGE_POINT_ID, "wrong"), id="wrong-password"),
        pytest.param(_basic("CP-OTHER", PASSWORD), id="wrong-username"),
        pytest.param({"Authorization": "Basic !!!"}, id="not-base64"),
        pytest.param({"Authorization": "Bearer abc"}, id="not-basic"),
        pytest.param(
            {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
            id="no-separator",
        ),
    ],
)
async def test_authentication_refuses(
    make_central_system: CentralSystemFactory,
    connect: Connect,
    headers: dict[str, str],
) -> None:
    central_system = await make_central_system(authenticate=_authenticate)

    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await connect(central_system, headers=headers)

    assert err.value.status == 401
    assert CHARGE_POINT_ID not in central_system.charge_points


async def test_authentication_lets_in(
    make_central_system: CentralSystemFactory, connect: Connect
) -> None:
    central_system = await make_central_system(authenticate=_authenticate)

    await connect(central_system, headers=_basic(CHARGE_POINT_ID, PASSWORD))

    assert central_system.charge_points[CHARGE_POINT_ID].connected


async def test_stop_disconnects_everyone(
    make_central_system: CentralSystemFactory, connect: Connect
) -> None:
    central_system = await make_central_system()
    fake = await connect(central_system)

    await central_system.async_stop()

    await until(lambda: fake.connection.ws.closed)
    assert not central_system.charge_points[CHARGE_POINT_ID].connected
    with pytest.raises(CsmsError):
        _ = central_system.port


async def test_start_twice_is_harmless(central_system: CentralSystem) -> None:
    port = central_system.port

    await central_system.async_start()

    assert central_system.port == port


async def test_port_needs_running_server() -> None:
    with pytest.raises(CsmsError):
        _ = CentralSystem().port


@pytest.fixture
def busy_port() -> Iterator[int]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


async def test_port_in_use_raises(busy_port: int) -> None:
    central_system = CentralSystem(host="127.0.0.1", port=busy_port)

    with pytest.raises(CsmsConnectionError):
        await central_system.async_start()


async def test_context_manager_starts_and_stops() -> None:
    async with CentralSystem(host="127.0.0.1", port=0) as central_system:
        assert central_system.port > 0

    with pytest.raises(CsmsError):
        _ = central_system.port


async def test_mounted_in_existing_application(
    session: aiohttp.ClientSession,
) -> None:
    central_system = CentralSystem()
    app = web.Application()
    app.router.add_get("/ocpp/{charge_point_id}", central_system.async_handle_request)
    async with TestServer(app, host="127.0.0.1") as server:
        ws = await session.ws_connect(
            server.make_url(f"/ocpp/{CHARGE_POINT_ID}"), protocols=("ocpp1.6",)
        )
        fake = FakeChargePoint(CHARGE_POINT_ID, ClientConnection(ws))
        await until(lambda: CHARGE_POINT_ID in central_system.charge_points)

        await ws.send_str('[2,"1","Heartbeat",{}]')
        reply = await ws.receive_json()

        assert reply[:2] == [3, "1"]
        await fake.close()


async def test_stopped_central_system_refuses_connections(
    session: aiohttp.ClientSession,
) -> None:
    central_system = CentralSystem()
    app = web.Application()
    app.router.add_get("/{charge_point_id}", central_system.async_handle_request)
    await central_system.async_stop()
    async with TestServer(app, host="127.0.0.1") as server:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await session.ws_connect(
                server.make_url(f"/{CHARGE_POINT_ID}"), protocols=("ocpp1.6",)
            )

    assert err.value.status == 503


def test_cli_reports_port_in_use(
    busy_port: int, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--host", "127.0.0.1", "--port", str(busy_port)])

    assert exit_info.value.code == 1
    assert "cannot listen" in capsys.readouterr().err
