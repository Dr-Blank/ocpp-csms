# ocpp-csms

An asyncio OCPP 1.6J central system (CSMS) for running EV chargers locally, built to be embedded in other software such as Home Assistant integrations.

Chargers connect to it over WebSocket. It answers their messages, keeps the latest state of each one, and gives you typed methods to send them requests. Message parsing and schema validation come from the [`ocpp`](https://github.com/mobilityhouse/ocpp) package; this library adds the server, the per-charger state and the API on top.

## Install

```bash
pip install ocpp-csms
```

Python 3.11 or newer. Depends on `aiohttp` and `ocpp`.

## Quick start

```python
import asyncio

from ocpp_csms import CentralSystem, ConnectedEvent, StatusEvent


async def main() -> None:
    async with CentralSystem(port=9321) as central_system:

        def on_event(event):
            if isinstance(event, ConnectedEvent):
                print("connected:", event.charge_point_id)
            elif isinstance(event, StatusEvent):
                print(event.charge_point_id, event.connector.status)

        central_system.add_listener(on_event)
        await asyncio.Event().wait()


asyncio.run(main())
```

Point the charger's central system URL at `ws://<host>:9321/` with its charge point id appended, for example `ws://192.0.2.10:9321/CP001`. Any path works: the last segment, percent-decoded, is taken as the charge point id. Chargers must offer the `ocpp1.6` WebSocket subprotocol if they offer any; one that offers none is let in, since many do not.

To try a charger without writing code:

```bash
ocpp-csms --port 9321
```

It lets every charger in and prints each event. `--debug` also logs every OCPP message in full, credentials included.

## Charge points

`central_system.charge_points` maps each charge point id to a `ChargePoint`. The object is created on first connection and kept across reconnections, so you can hold on to it.

State, as last reported by the charger:

| Attribute | What it holds |
|---|---|
| `connected` | Whether its WebSocket is open |
| `boot` | Vendor, model, serial, firmware, from `BootNotification` |
| `registration_status` | Answer given to the last `BootNotification` |
| `connectors` | `ConnectorState` per connector id; 0 is the whole charger |
| `transactions` | Running `Transaction` per transaction id |
| `get_transaction(connector_id)` | Running `Transaction` on a connector, or `None` |
| `meter_readings(connector_id)` | Latest `MeterReading` per measurand, phase and location |
| `get_meter_reading(connector_id, measurand)` | One reading, for example `"Power.Active.Import"` |
| `last_seen` | When it last sent anything |
| `firmware_status`, `diagnostics_status` | Last reported progress |

Requests, each returning the charger's answer:

| Method | OCPP request |
|---|---|
| `async_remote_start_transaction(id_tag, connector_id=None)` | `RemoteStartTransaction` |
| `async_remote_stop_transaction(transaction_id)` | `RemoteStopTransaction` |
| `async_reset(ResetType.SOFT)` | `Reset` |
| `async_change_availability(connector_id, AvailabilityType.INOPERATIVE)` | `ChangeAvailability` |
| `async_get_configuration(keys=None)` | `GetConfiguration` |
| `async_change_configuration(key, value)` | `ChangeConfiguration` |
| `async_trigger_message(MessageTrigger.STATUS_NOTIFICATION)` | `TriggerMessage` |
| `async_unlock_connector(connector_id)` | `UnlockConnector` |
| `async_data_transfer(vendor_id, message_id, data)` | `DataTransfer` |
| `async_call(request)` | Any `ocpp.v16.call` request, such as `SetChargingProfile` |

Requests raise `CsmsConnectionError` when the charger is not connected or drops before answering, `CsmsTimeoutError` (also a `TimeoutError`) when it does not answer in time, and `CsmsCallError` when it answers with an OCPP error. A status such as `Rejected` is returned, not raised, since what it means depends on the request.

**Changing configuration can strand the charger.** A wrong `CentralSystemURL`, `AuthorizationKey` or network setting leaves it unable to reach any central system until it is re-provisioned locally.

## Events

Listeners get one event object per thing that happened: `ConnectedEvent`, `DisconnectedEvent`, `BootEvent`, `StatusEvent`, `MeterValuesEvent`, `TransactionStartedEvent`, `TransactionStoppedEvent`, `FirmwareStatusEvent`, `DiagnosticsStatusEvent` and `SecurityEvent`. All carry `charge_point_id`.

Add a listener for every charger with `central_system.add_listener`, or for one with `charge_point.add_listener`. Both return a function that removes it.

Listeners, hooks and `DataTransfer` handlers run inside message handling, which reads one message at a time per charger. Keep them quick, and schedule any request to the same charger as a task rather than awaiting it there: its reply can only be read once they return.

## Deciding who gets in

With no hooks, every charger is let in and registered, and every id tag may charge. Pass async hooks to decide otherwise:

```python
from ocpp_csms import AuthorizationStatus, CentralSystem, RegistrationStatus

KNOWN = {"CP001": "example-password"}


async def authenticate(charge_point_id: str, password: str | None) -> bool:
    return KNOWN.get(charge_point_id) == password


async def registration(charge_point, info) -> RegistrationStatus:
    return RegistrationStatus.ACCEPTED


async def authorize(charge_point, id_tag: str) -> AuthorizationStatus:
    return AuthorizationStatus.ACCEPTED


central_system = CentralSystem(
    authenticate=authenticate, registration=registration, authorize=authorize
)
```

- `authenticate` runs before the WebSocket opens, with the password from the charger's HTTP Basic credentials (OCPP security profile 1), or `None` if it sent none. Return `False` to refuse with HTTP 401. Pass `ssl_context` as well to serve `wss://` (security profile 2).
- `registration` answers each `BootNotification`. `Pending` keeps the charger connected but unregistered, which lets you configure it before accepting it.
- `authorize` answers `Authorize` and `StartTransaction`.

## Vendor DataTransfer

Answer `DataTransfer` requests from chargers with a handler per vendor id:

```python
from ocpp_csms import DataTransferStatus


async def handle(charge_point, message_id, data):
    return DataTransferStatus.ACCEPTED, None


central_system.set_data_transfer_handler("ExampleVendor", handle)
```

Requests for a vendor without a handler are answered `UnknownVendorId`.

`ChargePoint.async_data_transfer` goes the other way. Its signature and its result, which unpacks as `status, data`, fit tunnels that carry a vendor protocol over `DataTransfer`, for example [`spinev-ble`](https://github.com/Dr-Blank/spinev-ble)'s `OcppTunnelTransport`:

```python
from spinev_ble import OcppTunnelTransport, SpinEvCharger

transport = OcppTunnelTransport(charge_point.async_data_transfer)
async with SpinEvCharger(transport) as charger:
    print(await charger.async_get_status())
```

Call `transport.notify_disconnected()` on the charger's `DisconnectedEvent`.

## Embedding in an aiohttp application

To serve chargers from an aiohttp application you already run, mount the handler instead of starting a server:

```python
app.router.add_get("/ocpp/{charge_point_id}", central_system.async_handle_request)
```

## Transactions

Transaction ids count up from the Unix time at which the `CentralSystem` was created, so a restart reuses no id unless sessions started faster than one a second on average. A session whose id tag was not accepted is still tracked, with the answer in `Transaction.id_tag_status`: the charger stops it, or keeps it running without charging if configured to.

State lives in memory. A charger reconnecting to a restarted central system usually does not boot again, so `boot` and `connectors` stay empty until it next reports them; send `async_trigger_message(MessageTrigger.BOOT_NOTIFICATION)` and `MessageTrigger.STATUS_NOTIFICATION` on `ConnectedEvent` to fill them at once. A session that started before the central system did is not in `transactions`, and stopping it raises no `TransactionStoppedEvent`. Without an `authenticate` hook, every charge point id that connects is kept for the life of the central system.

## Logging

The `ocpp_csms` logger records charge point ids and message names, never payloads, including when a message fails. The `ocpp_csms.wire` logger records every OCPP message, and the details of failures, in full at debug level. It stays off when `ocpp_csms` is set to debug, and must be set to debug by its own name. Messages carry credentials such as `AuthorizationKey` and, from some chargers, the WiFi password, so enable it only while debugging and do not share its output unredacted.

## Licence

[GNU Affero General Public License v3.0 or later](LICENSE). If you run a modified version of this library as a network service, you must offer its source to the users of that service.
