"""A real central system on a free port, and fake charge points that talk to it."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

import aiohttp
import pytest
from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call_result
from ocpp.v16.enums import Action

from ocpp_csms import CentralSystem, ChargePointEvent

CHARGE_POINT_ID = "CP001"

_HandlerT = TypeVar("_HandlerT", bound=Callable[..., Any])


def _on(action: str) -> Callable[[_HandlerT], _HandlerT]:
    decorator: Callable[[_HandlerT], _HandlerT] = on(action)
    return decorator


class ClientConnection:
    """``recv``/``send`` over an aiohttp client WebSocket."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws

    async def recv(self) -> str:
        msg = await self.ws.receive()
        if msg.type is aiohttp.WSMsgType.TEXT:
            return str(msg.data)
        raise ConnectionError("closed")

    async def send(self, message: str) -> None:
        await self.ws.send_str(message)


class FakeChargePoint(OcppChargePoint):  # type: ignore[misc]
    """Charge point side of OCPP, answering the central system's requests.

    ``replies`` maps each request action to the status it answers with.
    ``requests`` records every request received, as ``(action, payload)``.
    Set ``hang`` to leave requests unanswered.
    """

    def __init__(self, charge_point_id: str, connection: ClientConnection) -> None:
        super().__init__(charge_point_id, connection, response_timeout=2)
        self.connection = connection
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.replies: dict[str, str] = {}
        self.data_transfer_data: str | None = None
        self.configuration: list[dict[str, Any]] = []
        self.unknown_keys: list[str] = []
        self.hang = False

    async def _record(self, action: str, payload: dict[str, Any]) -> str:
        self.requests.append((action, payload))
        if self.hang:
            await asyncio.Event().wait()
        return self.replies.get(action, "Accepted")

    @_on(Action.data_transfer)
    async def on_data_transfer(self, **payload: Any) -> call_result.DataTransfer:
        status = await self._record("DataTransfer", payload)
        return call_result.DataTransfer(status=status, data=self.data_transfer_data)

    @_on(Action.remote_start_transaction)
    async def on_remote_start(
        self, **payload: Any
    ) -> call_result.RemoteStartTransaction:
        status = await self._record("RemoteStartTransaction", payload)
        return call_result.RemoteStartTransaction(status=status)

    @_on(Action.remote_stop_transaction)
    async def on_remote_stop(self, **payload: Any) -> call_result.RemoteStopTransaction:
        status = await self._record("RemoteStopTransaction", payload)
        return call_result.RemoteStopTransaction(status=status)

    @_on(Action.reset)
    async def on_reset(self, **payload: Any) -> call_result.Reset:
        status = await self._record("Reset", payload)
        return call_result.Reset(status=status)

    @_on(Action.change_availability)
    async def on_change_availability(
        self, **payload: Any
    ) -> call_result.ChangeAvailability:
        status = await self._record("ChangeAvailability", payload)
        return call_result.ChangeAvailability(status=status)

    @_on(Action.change_configuration)
    async def on_change_configuration(
        self, **payload: Any
    ) -> call_result.ChangeConfiguration:
        status = await self._record("ChangeConfiguration", payload)
        return call_result.ChangeConfiguration(status=status)

    @_on(Action.get_configuration)
    async def on_get_configuration(
        self, **payload: Any
    ) -> call_result.GetConfiguration:
        await self._record("GetConfiguration", payload)
        return call_result.GetConfiguration(
            configuration_key=self.configuration, unknown_key=self.unknown_keys
        )

    @_on(Action.trigger_message)
    async def on_trigger_message(self, **payload: Any) -> call_result.TriggerMessage:
        status = await self._record("TriggerMessage", payload)
        return call_result.TriggerMessage(status=status)

    @_on(Action.unlock_connector)
    async def on_unlock_connector(self, **payload: Any) -> call_result.UnlockConnector:
        status = await self._record("UnlockConnector", payload)
        return call_result.UnlockConnector(status=status)

    async def close(self) -> None:
        await self.connection.ws.close()


Connect = Callable[..., Awaitable[FakeChargePoint]]
CentralSystemFactory = Callable[..., Awaitable[CentralSystem]]


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
async def make_central_system() -> AsyncIterator[CentralSystemFactory]:
    """Start a central system on a free port, with any constructor options."""
    started: list[CentralSystem] = []

    async def make(**kwargs: Any) -> CentralSystem:
        central_system = CentralSystem(host="127.0.0.1", port=0, **kwargs)
        await central_system.async_start()
        started.append(central_system)
        return central_system

    yield make
    for central_system in started:
        await central_system.async_stop()


@pytest.fixture
async def central_system(make_central_system: CentralSystemFactory) -> CentralSystem:
    return await make_central_system()


@pytest.fixture
def events(central_system: CentralSystem) -> list[ChargePointEvent]:
    received: list[ChargePointEvent] = []
    central_system.add_listener(received.append)
    return received


@pytest.fixture
async def connect(
    session: aiohttp.ClientSession,
) -> AsyncIterator[Callable[..., Awaitable[FakeChargePoint]]]:
    """Open a fake charge point's connection to a central system."""
    tasks: list[asyncio.Task[None]] = []
    fakes: list[FakeChargePoint] = []

    async def open_connection(
        central_system: CentralSystem,
        charge_point_id: str = CHARGE_POINT_ID,
        **kwargs: Any,
    ) -> FakeChargePoint:
        kwargs.setdefault("protocols", ("ocpp1.6",))
        ws = await session.ws_connect(
            f"ws://127.0.0.1:{central_system.port}/ocpp/{charge_point_id}", **kwargs
        )
        fake = FakeChargePoint(charge_point_id, ClientConnection(ws))
        tasks.append(asyncio.create_task(_serve(fake)))
        fakes.append(fake)
        # Let the central system register the connection.
        await until(lambda: charge_point_id in central_system.charge_points)
        await until(lambda: central_system.charge_points[charge_point_id].connected)
        return fake

    yield open_connection
    for fake in fakes:
        await fake.close()
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _serve(fake: FakeChargePoint) -> None:
    with contextlib.suppress(ConnectionError):
        await fake.start()


async def until(condition: Callable[[], bool]) -> None:
    """Wait up to two seconds for ``condition`` to hold."""
    async with asyncio.timeout(2):
        # State changes inside the library signal nothing a test can await.
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
