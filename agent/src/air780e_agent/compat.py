"""Shareable, read-only compatibility evidence for one Agent host.

The report deliberately omits IMEI, ICCID, SMSC, host name and network name.
It also never lists, reads or deletes stored SMS.  That makes the resulting
JSON suitable for attaching to an issue or committing as matrix evidence.
"""

from __future__ import annotations

import glob
import grp
import platform
import re
import shutil
import stat
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .at import ATClient, ATError, SerialTransport, Transport

REPORT_SCHEMA_VERSION = 1
DEFAULT_PORT_PATTERN = "/dev/ttyACM*"
GENERIC_AIR780E_USB_SERIAL = "000000000001"
SHAREABLE_SERIAL_GROUPS = {"dialout", "root", "tty", "uucp"}

TransportFactory = Callable[[str], Transport]
PortInspector = Callable[[str], Awaitable[dict[str, Any]]]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _udev_ignore_rule_present() -> bool:
    for root in (Path("/etc/udev/rules.d"), Path("/usr/lib/udev/rules.d")):
        try:
            rules = root.glob("*.rules")
        except OSError:
            continue
        for path in rules:
            text = _read_text(path)
            if "19d1" in text and (
                "ID_MM_DEVICE_IGNORE" in text or "ID_MM_PORT_IGNORE" in text
            ):
                return True
    return False


def _modem_manager_state(installed: bool) -> str:
    if not installed:
        return "not-installed"
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "unknown"
    try:
        result = subprocess.run(
            [systemctl, "is-active", "ModemManager.service"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    state = result.stdout.strip()
    return state if state in {"active", "inactive", "failed", "activating"} else "unknown"


def collect_host_facts() -> dict[str, Any]:
    """Return non-identifying OS facts relevant to serial compatibility."""
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    modem_manager_installed = shutil.which("mmcli") is not None
    return {
        "os": release.get("ID", platform.system().lower()),
        "os_version": release.get("VERSION_ID") or release.get("BUILD_ID", ""),
        "os_pretty_name": release.get("PRETTY_NAME", platform.system()),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "cdc_acm_loaded": Path("/sys/module/cdc_acm").exists(),
        "modem_manager_cli_installed": modem_manager_installed,
        "modem_manager_service": _modem_manager_state(modem_manager_installed),
        "modem_manager_ignore_rule_detected": _udev_ignore_rule_present(),
    }


def collect_acm_interfaces(
    *,
    sysfs_root: Path = Path("/sys/class/tty"),
    device_root: Path = Path("/dev"),
) -> list[dict[str, Any]]:
    """Describe every kernel-enumerated ttyACM interface without opening it."""
    interfaces: list[dict[str, Any]] = []
    for entry in sorted(sysfs_root.glob("ttyACM*")):
        name = entry.name
        device_node = device_root / name
        try:
            interface = (entry / "device").resolve(strict=True)
        except OSError:
            interface = None

        usb = None
        if interface is not None:
            for parent in (interface, *interface.parents):
                if (parent / "idVendor").is_file():
                    usb = parent
                    break

        serial = _read_text(usb / "serial") if usb is not None else ""
        row: dict[str, Any] = {
            "name": name,
            "device_node": str(device_node),
            "device_node_present": device_node.exists(),
            "interface_number": (
                _read_text(interface / "bInterfaceNumber")
                if interface is not None
                else ""
            ),
            "usb_path": usb.name if usb is not None else "",
            "usb_vid": _read_text(usb / "idVendor") if usb is not None else "",
            "usb_pid": _read_text(usb / "idProduct") if usb is not None else "",
            "usb_manufacturer": (
                _read_text(usb / "manufacturer") if usb is not None else ""
            ),
            "usb_product": _read_text(usb / "product") if usb is not None else "",
            # Some boards expose a unique serial.  Record only the property
            # needed by discovery policy, never its raw value.
            "usb_serial_available": bool(serial),
            "usb_serial_is_generic_air780e": serial == GENERIC_AIR780E_USB_SERIAL,
        }
        if device_node.exists():
            try:
                node_stat = device_node.stat()
                row["device_mode"] = f"{stat.S_IMODE(node_stat.st_mode):04o}"
                try:
                    group = grp.getgrgid(node_stat.st_gid).gr_name
                    row["device_group"] = (
                        group if group in SHAREABLE_SERIAL_GROUPS else "other"
                    )
                except KeyError:
                    row["device_group"] = "other"
            except OSError:
                pass
        interfaces.append(row)
    return interfaces


def _identity_value(line: str, prefix: str) -> str:
    if line.upper().startswith(prefix.upper() + ":"):
        return line.split(":", 1)[1].strip()
    return line.strip()


def _registration_state(line: str) -> int | None:
    if not line:
        return None
    _, _, value = line.partition(":")
    fields = [field.strip() for field in value.split(",")]
    candidate = fields[1] if len(fields) > 1 else fields[0]
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _storage(line: str) -> dict[str, Any]:
    match = re.search(r'\+CPMS:\s*"([^"]+)",\s*(\d+),\s*(\d+)', line)
    if match is None:
        return {"name": "", "used": None, "capacity": None}
    return {
        "name": match.group(1),
        "used": int(match.group(2)),
        "capacity": int(match.group(3)),
    }


async def inspect_compatibility_port(
    port: str,
    *,
    timeout: float = 3.0,
    transport_factory: TransportFactory = SerialTransport,
) -> dict[str, Any]:
    """Run identification and status queries, with no SMS storage mutation."""
    report: dict[str, Any] = {
        "port": port,
        "open": False,
        "at_response": False,
        "manufacturer": "",
        "model": "",
        "firmware": "",
        "ati": "",
        "imei_available": False,
        "iccid_available": False,
        "smsc_configured": False,
        "sim_pin_state": "",
        "storage": {"name": "", "used": None, "capacity": None},
        "radio_enabled": None,
        "registration": {"eps": None, "cs": None, "registered": None},
        "signal": {"rssi": None, "ber": None},
        "commands": {},
    }
    client = ATClient(transport_factory(port), name=f"compat:{port}")
    try:
        await client.open()
        report["open"] = True
    except ATError as exc:
        report["open_error"] = type(exc).__name__
        return report

    async def query(command: str) -> str:
        try:
            response = await client.execute(command, timeout=timeout)
        except ATError as exc:
            report["commands"][command] = type(exc).__name__
            return ""
        report["commands"][command] = "ok"
        value = response.lines[0] if response.lines else ""
        return value

    try:
        ati = await query("ATI")
        report["at_response"] = report["commands"].get("ATI") == "ok"
        if not report["at_response"]:
            return report

        cgmi = await query("AT+CGMI")
        cgmm = await query("AT+CGMM")
        cgmr = await query("AT+CGMR")
        imei = await query("AT+CGSN")
        iccid = await query("AT+ICCID")
        pin = await query("AT+CPIN?")
        cpms = await query("AT+CPMS?")
        cfun = await query("AT+CFUN?")
        cereg = await query("AT+CEREG?")
        creg = await query("AT+CREG?")
        csq = await query("AT+CSQ")
        smsc = await query("AT+CSCA?")

        report["ati"] = ati
        report["manufacturer"] = _identity_value(cgmi, "+CGMI")
        report["model"] = _identity_value(cgmm, "+CGMM") or ati
        report["firmware"] = _identity_value(cgmr, "+CGMR")
        report["imei_available"] = bool(_identity_value(imei, "+CGSN"))
        report["iccid_available"] = bool(_identity_value(iccid, "+ICCID"))
        report["smsc_configured"] = bool(re.search(r'"[^"]+"', smsc))
        report["sim_pin_state"] = _identity_value(pin, "+CPIN")
        report["storage"] = _storage(cpms)

        cfun_match = re.search(r"\+CFUN:\s*(\d+)", cfun)
        if cfun_match is not None:
            report["radio_enabled"] = cfun_match.group(1) == "1"

        eps = _registration_state(cereg)
        cs = _registration_state(creg)
        report["registration"] = {
            "eps": eps,
            "cs": cs,
            "registered": (
                None if eps is None and cs is None else eps in {1, 5} or cs in {1, 5}
            ),
        }

        csq_match = re.search(r"\+CSQ:\s*(\d+)\s*,\s*(\d+)", csq)
        if csq_match is not None:
            report["signal"] = {
                "rssi": int(csq_match.group(1)),
                "ber": int(csq_match.group(2)),
            }
        return report
    finally:
        # No modem background tasks were started, and no CMG* command was
        # issued.  Closing here releases the port without touching messages.
        await client.close()


async def build_compatibility_report(
    *,
    pattern: str = DEFAULT_PORT_PATTERN,
    ports: list[str] | None = None,
    inspector: PortInspector | None = None,
    host: dict[str, Any] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect one deterministic report suitable for the public matrix."""
    candidates = sorted(set(ports if ports is not None else glob.glob(pattern)))
    inspect_one = inspector or inspect_compatibility_port
    port_reports = [await inspect_one(port) for port in candidates]
    acm_interfaces = interfaces if interfaces is not None else collect_acm_interfaces()
    at_ports = [row for row in port_reports if row.get("at_response")]
    firmwares = sorted({str(row.get("firmware")) for row in at_ports if row.get("firmware")})
    usb_devices = {
        row.get("usb_path") for row in acm_interfaces if row.get("usb_path")
    }
    validation_ready = any(row.get("firmware") for row in at_ports)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent_version": __version__,
        "redaction": {
            "shareable_by_default": True,
            "omitted": ["hostname", "IMEI", "ICCID", "SMSC", "operator"],
        },
        "host": host if host is not None else collect_host_facts(),
        "acm_interfaces": acm_interfaces,
        "ports": port_reports,
        "summary": {
            "candidate_device_nodes": len(candidates),
            "enumerated_acm_interfaces": len(acm_interfaces),
            "usb_devices": len(usb_devices),
            "at_ports": len(at_ports),
            "firmwares": firmwares,
            "validation_ready": validation_ready,
        },
    }
