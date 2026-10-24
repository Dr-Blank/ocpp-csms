## What this project is

Published PyPI lib: asyncio OCPP 1.6J central system (CSMS), built to be embedded in other software, first of all Home Assistant integrations and a future core `ocpp` hub. Vendor neutral: nothing charger specific lives here; vendor protocols ride on `DataTransfer` through `set_data_transfer_handler` and `ChargePoint.async_data_transfer`. People install this, build on it — public API contract, not draft.

Layers:

- `server.py`: `CentralSystem`, the aiohttp WebSocket server. URL routing (last path segment is the charge point id), subprotocol check, HTTP Basic auth, hooks, listeners.
- `charge_point.py`: `ChargePoint`, one per id, kept across reconnections. State, request methods, and `_OcppHandler`, the `ocpp` package subclass answering incoming messages.
- `models.py`: frozen dataclasses for state and events. `const.py`: defaults and our own `StrEnum`s. `exceptions.py`: `CsmsError` tree.

Licence AGPL-3.0-or-later.

## The ocpp package

Message parsing, routing and schema validation come from mobilityhouse `ocpp`. It ships no type information, so:

- Public API never exposes its types. Our own enums and dataclasses in, `str` statuses out. `async_call` is the one escape hatch, typed `Any`.
- Decorate handlers with the typed `_on` wrapper, not `ocpp.routing.on` directly.
- Its `ChargePoint.start()` reads messages one at a time, so a handler or listener that awaits a request to the same charge point deadlocks. Listeners are sync; hooks may await anything else.

## Never leak secrets

No real credential or device identifier goes into code, tests, docs, comments, commit messages, fixtures: authorization keys, passwords, charge point ids, serial numbers, IP addresses, captures. Use obvious placeholders: `CP001`, `example-password`, `192.0.2.x`, `SN-0000`.

Payloads carry credentials (`AuthorizationKey`, vendor `DataTransfer` data). The `ocpp_csms` logger logs ids and message names, never payloads. Full messages only on `ocpp_csms.wire`, at debug level; a test guards that payloads stay out of info logs.

## Comments and docs describe the library, not its history

Every comment, docstring, README line, error message says what something is and how to use it. Nothing says how the library came to exist, what it was verified against, or that AI wrote any of it. No changelog narration in code. Safety warnings stay, as statements about consequences.

## Coding practices

- Public names carry docstrings, with the string after the assignment for constants, enum members and dataclass fields.
- Type everything. `mypy --strict` covers `src` and `tests`, must pass.
- Raise package exceptions from `exceptions.py`, never bare `ValueError` or `RuntimeError`.
- No `assert` for runtime invariants in library code. Raise instead.
- A charger answering `Rejected` or similar is a returned status, not an exception. Exceptions are for no answer, a dropped connection, or an OCPP error.
- New behaviour comes with tests against the real server on a free port, driven by `FakeChargePoint` in `tests/conftest.py`. Never against real hardware.

## Gates

All must pass before work done:

```bash
uv run pytest
uv run prek run --all-files
uv run pylint src/ocpp_csms
```

## Git

Don't commit, push, tag unless explicitly asked. Prep changes in working tree, describe them.
