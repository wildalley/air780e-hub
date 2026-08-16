"""Point this at a serial port and it tells you what is on the other end.

Doubles as the hardware bring-up tool: the checklist in
``docs/at-reference.md`` is exactly what ``probe`` runs.

    python -m air780e_agent.probe /dev/ttyACM3
    python -m air780e_agent.probe /dev/ttyACM3 --listen
    python -m air780e_agent.probe /dev/ttyACM3 --send 10086 CXHF
    python -m air780e_agent.probe --scan          # try every /dev/ttyACM*
    air780e-probe --report /tmp/air780e-compat.json
    air780e-probe --report /tmp/air780e-enum.json --enumeration-only
    air780e-probe --report /tmp/air780e-hotplug.json --observe-hotplug 120
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import sys
from pathlib import Path

from .at import ATClient, ATError, SerialTransport
from .compat import DEFAULT_PORT_PATTERN, build_compatibility_report, observe_hotplug
from .modem import Air780E
from .pdu import DecodedSms

log = logging.getLogger("probe")


async def _identify(port: str, timeout: float = 3.0) -> str | None:
    """Return the model string if this port answers ATI, else None."""
    client = ATClient(SerialTransport(port), name=port)
    try:
        await client.open()
    except ATError:
        return None
    try:
        response = await client.execute("ATI", timeout=timeout)
        return response.lines[0] if response.lines else "(no identity)"
    except ATError:
        return None
    finally:
        await client.close()


async def scan(pattern: str = DEFAULT_PORT_PATTERN) -> None:
    ports = sorted(glob.glob(pattern))
    if not ports:
        print(f"no ports matched {pattern}")
        print("plug the module in and check `dmesg | tail -30`")
        return

    print(f"probing {len(ports)} port(s)…\n")
    for port in ports:
        model = await _identify(port)
        if model is None:
            print(f"  {port:<16} no AT response")
        else:
            print(f"  {port:<16} {model}   <-- AT port")
    print(
        "\nIf nothing answered, the module is probably running LuatOS firmware\n"
        "rather than AT firmware — reflash with LuaTools."
    )


async def inspect(port: str, *, listen: bool, send: tuple[str, str] | None) -> int:
    received: list[DecodedSms] = []

    def on_sms(sms: DecodedSms) -> None:
        received.append(sms)
        stamp = sms.timestamp.isoformat() if sms.timestamp else "?"
        print(f"\n[SMS] {sms.address}  {stamp}\n      {sms.text}\n")

    client = ATClient(SerialTransport(port), name=port)
    try:
        await client.open()
    except ATError as exc:
        print(f"cannot open {port}: {exc}", file=sys.stderr)
        return 1

    modem = Air780E(client, on_sms=on_sms)
    try:
        info = await modem.initialize()
    except ATError as exc:
        print(f"{port} did not respond as an AT modem: {exc}", file=sys.stderr)
        await client.close()
        return 1

    signal = await modem.read_signal()
    used, capacity = await modem.storage_usage()

    print(f"port        {port}")
    print(f"manufacturer {info.manufacturer or '(unavailable)'}")
    print(f"model       {info.hardware_model or info.model}")
    print(f"firmware    {info.firmware or '(unavailable)'}")
    if info.model and info.model != info.hardware_model:
        print(f"ATI         {info.model}")
    print(f"IMEI        {info.imei or '(unavailable)'}")
    print(f"ICCID       {info.iccid or '(no SIM?)'}")
    print(f"SMSC        {info.smsc or '(unset — sending will fail)'}")
    print(f"operator    {info.operator or '(not registered)'}")
    print(f"registered  {'yes' if info.registered else 'NO'}")
    if signal.dbm is not None:
        print(f"signal      {signal.rssi} ({signal.dbm} dBm, {signal.bars}/5 bars)")
    else:
        print("signal      unknown")
    if signal.rsrp is not None:
        print(f"rsrp/rsrq   {signal.rsrp} / {signal.rsrq}")
    print(f"storage     {used}/{capacity} used")

    if used:
        print(f"\ndraining {used} stored message(s)…")
        for sms in await modem.drain_inbox():
            print(f"  {sms.address}: {sms.text[:70]}")

    if send is not None:
        number, text = send
        print(f"\nsending to {number}: {text!r}")
        try:
            refs = await modem.send_sms(number, text)
            print(f"  accepted, message reference(s): {refs}")
        except ATError as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            if not info.smsc:
                print("  (no SMSC configured — check AT+CSCA?)", file=sys.stderr)

    if listen:
        print("\nlistening for incoming messages, Ctrl-C to stop…")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    await modem.close()
    await client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="air780e-probe",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("port", nargs="?", help="serial device, e.g. /dev/ttyACM3")
    parser.add_argument(
        "--scan", action="store_true", help="try every /dev/ttyACM* and report which speaks AT"
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="write a redacted, read-only compatibility JSON report ('-' for stdout)",
    )
    parser.add_argument(
        "--enumeration-only",
        action="store_true",
        help="with --report, inspect only sysfs/udev and never open serial ports",
    )
    parser.add_argument(
        "--observe-hotplug",
        type=float,
        metavar="SECONDS",
        help="with --report, record changed sysfs/udev topology for this long",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="hotplug polling interval (default: 0.5)",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PORT_PATTERN,
        help=f"serial glob used by --scan/--report (default: {DEFAULT_PORT_PATTERN})",
    )
    parser.add_argument(
        "--listen", action="store_true", help="stay attached and print incoming messages"
    )
    parser.add_argument(
        "--send", nargs=2, metavar=("NUMBER", "TEXT"), help="send one message, then continue"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if (args.enumeration_only or args.observe_hotplug is not None) and not args.report:
        parser.error("--enumeration-only/--observe-hotplug requires --report")
    if (args.enumeration_only or args.observe_hotplug is not None) and args.port:
        parser.error("a serial port cannot be combined with enumeration-only reporting")
    if args.observe_hotplug is not None and args.observe_hotplug <= 0:
        parser.error("--observe-hotplug must be greater than zero")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than zero")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    if args.report:
        if args.observe_hotplug is not None:
            report = asyncio.run(
                observe_hotplug(
                    duration=args.observe_hotplug,
                    poll_interval=args.poll_interval,
                )
            )
        else:
            ports = [args.port] if args.port else None
            report = asyncio.run(
                build_compatibility_report(
                    pattern=args.pattern,
                    ports=ports,
                    enumeration_only=args.enumeration_only,
                )
            )
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.report == "-":
            print(rendered, end="")
        else:
            try:
                Path(args.report).write_text(rendered, encoding="utf-8")
            except OSError as exc:
                print(f"cannot write {args.report}: {exc}", file=sys.stderr)
                return 1
            summary = report["summary"]
            if report.get("report_type") == "hotplug-observation":
                print(
                    f"wrote {args.report}: {summary['topology_transitions']} topology "
                    f"transition(s), hotplug cycle "
                    f"{'complete' if summary['hotplug_cycle_complete'] else 'incomplete'}"
                )
            elif args.enumeration_only:
                print(
                    f"wrote {args.report}: {summary['air780e_usb_devices']} Air780E "
                    f"USB device(s), {summary['complete_air780e_layouts']} complete layout(s)"
                )
            else:
                print(
                    f"wrote {args.report}: {summary['at_ports']} AT port(s), "
                    f"{summary['air780e_usb_devices']} Air780E USB device(s)"
                )
        if report.get("report_type") == "hotplug-observation":
            ready_key = "hotplug_cycle_complete"
        elif args.enumeration_only:
            ready_key = "enumeration_ready"
        else:
            ready_key = "validation_ready"
        return 0 if report["summary"][ready_key] else 1

    if args.scan or not args.port:
        if not args.scan:
            parser.print_help()
            return 2
        asyncio.run(scan(args.pattern))
        return 0

    send = tuple(args.send) if args.send else None
    try:
        return asyncio.run(inspect(args.port, listen=args.listen, send=send))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
