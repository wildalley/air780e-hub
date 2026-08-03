"""Agent entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .app import AgentApp
from .config import EXAMPLE_CONFIG, AgentConfig, ConfigError

log = logging.getLogger("air780e-agent")


async def _run(config: AgentConfig) -> int:
    app = AgentApp(config)
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:  # not on Linux; fall back to KeyboardInterrupt
            pass

    runner = asyncio.create_task(app.run())
    await stopping.wait()
    await app.stop()
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="air780e-agent")
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--print-example-config", action="store_true",
        help="write a starter config to stdout and exit",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="validate the config and exit without touching any hardware",
    )
    args = parser.parse_args(argv)

    if args.print_example_config:
        print(EXAMPLE_CONFIG, end="")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    try:
        config = AgentConfig.load(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        print("\nStart from a template:\n  air780e-agent --print-example-config",
              file=sys.stderr)
        return 2

    if args.check:
        print(f"agent id     {config.agent_id}")
        print(f"database     {config.db_path}")
        print(f"server       {config.server.url or '(standalone)'}")
        for device in config.devices:
            exists = "ok" if Path(device.port).exists() else "MISSING"
            print(f"device {device.name:<8} {device.port}  [{exists}]")
        return 0

    try:
        return asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
