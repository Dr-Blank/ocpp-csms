"""Command line central system that prints what charge points send.

Run ``ocpp-csms --port 9321`` and point a charge point at
``ws://<this host>:9321/<charge point id>``. Every charge point is let in and
every event is printed. Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Sequence

from .const import DEFAULT_PORT
from .exceptions import CsmsError
from .models import ChargePointEvent
from .server import CentralSystem


def _print_event(event: ChargePointEvent) -> None:
    print(event, flush=True)


async def _serve(host: str | None, port: int) -> None:
    async with CentralSystem(host=host, port=port) as central_system:
        central_system.add_listener(_print_event)
        print(f"listening on port {central_system.port}", flush=True)
        await asyncio.Event().wait()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line central system. Returns the exit status."""
    parser = argparse.ArgumentParser(
        prog="ocpp-csms", description="OCPP 1.6J central system that prints events"
    )
    parser.add_argument("--host", help="address to listen on (default: every one)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="port to listen on"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log every OCPP message in full, credentials included",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_serve(args.host, args.port))
    except CsmsError as err:
        parser.exit(1, f"ocpp-csms: {err}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
