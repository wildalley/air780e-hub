"""Shareable, read-only compatibility evidence for one Agent host.

The report deliberately omits IMEI, ICCID, SMSC, host name and network name.
It also never lists, reads or deletes stored SMS.  That makes the resulting
JSON suitable for attaching to an issue or committing as matrix evidence.
"""

from __future__ import annotations

import asyncio
import glob
import grp
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .at import ATClient, ATError, SerialTransport, Transport

REPORT_SCHEMA_VERSION = 2
DEFAULT_PORT_PATTERN = "/dev/ttyACM*"
GENERIC_AIR780E_USB_SERIAL = "000000000001"
SHAREABLE_SERIAL_GROUPS = {"dialout", "root", "tty", "uucp"}
AIR780E_USB_VID = "19d1"
AIR780E_USB_PID = "0001"
AIR780E_ACM_INTERFACES = ("02", "04", "06")

TransportFactory = Callable[[str], Transport]
PortInspector = Callable[[str], Awaitable[dict[str, Any]]]
InterfaceCollector = Callable[[], list[dict[str, Any]]]
Sleeper = Callable[[float], Awaitable[None]]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _udev_properties(devnum: str, udev_data_root: Path) -> dict[str, str] | None:
    """Read the small applied-property subset associated with one device."""
    if re.fullmatch(r"\d+:\d+", devnum) is None:
        return None
    try:
        text = (udev_data_root / f"c{devnum}").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    properties: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("E:") and "=" in line:
            key, value = line[2:].split("=", 1)
            properties[key] = value
    return properties


def _udev_flag(properties: dict[str, str] | None, key: str) -> bool | None:
    if properties is None:
        return None
    return properties.get(key) == "1"


def _udev_ignore_rule_present() -> bool:
    for root in (Path("/etc/udev/rules.d"), Path("/usr/lib/udev/rules.d")):
        try:
            rules = root.glob("*.rules")
        except OSError:
            continue
        for path in rules:
            text = _read_text(path)
            if "19d1" in text and ("ID_MM_DEVICE_IGNORE" in text or "ID_MM_PORT_IGNORE" in text):
                return True
    return False


def _systemd_service_state(service: str) -> str:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "unknown"
    try:
        result = subprocess.run(
            [systemctl, "is-active", service],
            capture_output=True,
            check=False,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    state = result.stdout.strip()
    return state if state in {"active", "inactive", "failed", "activating"} else "unknown"


def _modem_manager_state(installed: bool) -> str:
    if not installed:
        return "not-installed"
    return _systemd_service_state("ModemManager.service")


def _boot_session_hash(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str:
    boot_id = _read_text(path)
    if not boot_id:
        return ""
    return hashlib.sha256(boot_id.encode("ascii", errors="ignore")).hexdigest()[:12]


def _uptime_seconds(path: Path = Path("/proc/uptime")) -> int | None:
    value = _read_text(path).partition(" ")[0]
    try:
        return int(float(value))
    except (OverflowError, ValueError):
        return None


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
        "boot_session_hash": _boot_session_hash(),
        "uptime_seconds": _uptime_seconds(),
        "cdc_acm_loaded": Path("/sys/module/cdc_acm").exists(),
        "air780e_agent_service": _systemd_service_state("air780e-agent.service"),
        "modem_manager_cli_installed": modem_manager_installed,
        "modem_manager_service": _modem_manager_state(modem_manager_installed),
        "modem_manager_ignore_rule_detected": _udev_ignore_rule_present(),
    }


def collect_acm_interfaces(
    *,
    sysfs_root: Path = Path("/sys/class/tty"),
    device_root: Path = Path("/dev"),
    udev_data_root: Path = Path("/run/udev/data"),
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
        device_node_present = device_node.exists()
        device_node_readable = device_node_present and os.access(device_node, os.R_OK)
        device_node_writable = device_node_present and os.access(device_node, os.W_OK)
        port_properties = _udev_properties(_read_text(entry / "dev"), udev_data_root)
        device_properties = (
            _udev_properties(_read_text(usb / "dev"), udev_data_root) if usb is not None else None
        )
        row: dict[str, Any] = {
            "name": name,
            "device_node": str(device_node),
            "device_node_present": device_node_present,
            "device_node_readable": device_node_readable,
            "device_node_writable": device_node_writable,
            "device_node_ready": device_node_readable and device_node_writable,
            "interface_number": (
                _read_text(interface / "bInterfaceNumber") if interface is not None else ""
            ),
            "usb_path": usb.name if usb is not None else "",
            "usb_vid": _read_text(usb / "idVendor") if usb is not None else "",
            "usb_pid": _read_text(usb / "idProduct") if usb is not None else "",
            "usb_manufacturer": (_read_text(usb / "manufacturer") if usb is not None else ""),
            "usb_product": _read_text(usb / "product") if usb is not None else "",
            # Some boards expose a unique serial.  Record only the property
            # needed by discovery policy, never its raw value.
            "usb_serial_available": bool(serial),
            "usb_serial_is_generic_air780e": serial == GENERIC_AIR780E_USB_SERIAL,
            # These values come from udev's runtime database, so they prove
            # that a rule was applied to this device rather than merely found
            # in a rules file.
            "udev_port_properties_available": port_properties is not None,
            "udev_device_properties_available": device_properties is not None,
            "modem_manager_port_ignore": _udev_flag(port_properties, "ID_MM_PORT_IGNORE"),
            "modem_manager_device_ignore": _udev_flag(device_properties, "ID_MM_DEVICE_IGNORE"),
        }
        if device_node_present:
            try:
                node_stat = device_node.stat()
                row["device_mode"] = f"{stat.S_IMODE(node_stat.st_mode):04o}"
                try:
                    group = grp.getgrgid(node_stat.st_gid).gr_name
                    row["device_group"] = group if group in SHAREABLE_SERIAL_GROUPS else "other"
                except KeyError:
                    row["device_group"] = "other"
            except OSError:
                pass
        interfaces.append(row)
    return interfaces


def _group_ignore_state(rows: list[dict[str, Any]]) -> tuple[bool | None, bool | None]:
    device_values = [row.get("modem_manager_device_ignore") for row in rows]
    port_values = [row.get("modem_manager_port_ignore") for row in rows]

    device_ignore: bool | None
    if any(value is True for value in device_values):
        device_ignore = True
    elif any(value is False for value in device_values):
        device_ignore = False
    else:
        device_ignore = None

    port_ignore: bool | None
    if port_values and all(value is True for value in port_values):
        port_ignore = True
    elif any(value is False for value in port_values):
        port_ignore = False
    else:
        port_ignore = None
    return device_ignore, port_ignore


def group_acm_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group tty rows by physical USB device and validate Air780E layout."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for index, row in enumerate(interfaces):
        usb_path = str(row.get("usb_path") or "")
        # Do not collapse unrelated unresolved tty rows into one fake device.
        identity = usb_path or f"unresolved:{index}:{row.get('name', '')}"
        key = (
            identity,
            str(row.get("usb_vid") or "").lower(),
            str(row.get("usb_pid") or "").lower(),
        )
        grouped.setdefault(key, []).append(row)

    devices: list[dict[str, Any]] = []
    for (_identity, vid, pid), rows in sorted(grouped.items()):
        rows = sorted(
            rows,
            key=lambda row: (
                str(row.get("interface_number") or ""),
                str(row.get("name") or ""),
            ),
        )
        interface_numbers = sorted(
            {str(row.get("interface_number")) for row in rows if row.get("interface_number")}
        )
        is_air780e = vid == AIR780E_USB_VID and pid == AIR780E_USB_PID
        device_ignore, port_ignore = _group_ignore_state(rows)
        ignore_applied: bool | None
        if device_ignore is True or port_ignore is True:
            ignore_applied = True
        elif device_ignore is False and port_ignore is False:
            ignore_applied = False
        else:
            ignore_applied = None

        devices.append(
            {
                "usb_path": str(rows[0].get("usb_path") or ""),
                "usb_vid": vid,
                "usb_pid": pid,
                "is_air780e": is_air780e,
                "interface_numbers": interface_numbers,
                "interfaces": [
                    {
                        "name": str(row.get("name") or ""),
                        "device_node": str(row.get("device_node") or ""),
                        "device_node_present": row.get("device_node_present") is True,
                        "device_node_readable": row.get("device_node_readable") is True,
                        "device_node_writable": row.get("device_node_writable") is True,
                        "device_node_ready": row.get("device_node_ready") is True,
                        "interface_number": str(row.get("interface_number") or ""),
                    }
                    for row in rows
                ],
                "complete_acm_layout": (
                    is_air780e
                    and len(rows) == len(AIR780E_ACM_INTERFACES)
                    and interface_numbers == list(AIR780E_ACM_INTERFACES)
                ),
                "device_nodes_ready": bool(rows)
                and all(row.get("device_node_ready") is True for row in rows),
                "modem_manager_device_ignore": device_ignore,
                "modem_manager_all_ports_ignore": port_ignore,
                "modem_manager_ignore_applied": ignore_applied,
            }
        )
    return devices


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
            "registered": (None if eps is None and cs is None else eps in {1, 5} or cs in {1, 5}),
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


def _redaction() -> dict[str, Any]:
    return {
        "shareable_by_default": True,
        "omitted": ["hostname", "IMEI", "ICCID", "SMSC", "operator", "USB serial"],
    }


def _modem_manager_safety(host: dict[str, Any], air780e_devices: list[dict[str, Any]]) -> str:
    if host.get("modem_manager_cli_installed") is False:
        return "not-installed"
    if not air780e_devices:
        return "unknown"
    if all(row.get("modem_manager_ignore_applied") is True for row in air780e_devices):
        return "ignore-applied"

    service = host.get("modem_manager_service")
    if service in {"inactive", "failed"}:
        return "service-inactive"
    if service == "active" and any(
        row.get("modem_manager_ignore_applied") is False for row in air780e_devices
    ):
        return "unprotected"
    return "unknown"


def _enumeration_summary(host: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any]:
    air780e_devices = [row for row in devices if row.get("is_air780e")]
    complete_layouts = sum(row.get("complete_acm_layout") is True for row in air780e_devices)
    device_nodes_ready = bool(air780e_devices) and all(
        row.get("device_nodes_ready") is True for row in air780e_devices
    )
    enumeration_ready = (
        host.get("cdc_acm_loaded") is True
        and bool(air780e_devices)
        and complete_layouts == len(air780e_devices)
        and device_nodes_ready
    )
    return {
        "usb_devices": len(devices),
        "air780e_usb_devices": len(air780e_devices),
        "complete_air780e_layouts": complete_layouts,
        "device_nodes_ready": device_nodes_ready,
        "enumeration_ready": enumeration_ready,
        "modem_manager_safety": _modem_manager_safety(host, air780e_devices),
    }


async def build_compatibility_report(
    *,
    pattern: str = DEFAULT_PORT_PATTERN,
    ports: list[str] | None = None,
    inspector: PortInspector | None = None,
    host: dict[str, Any] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
    enumeration_only: bool = False,
) -> dict[str, Any]:
    """Collect one deterministic report suitable for the public matrix."""
    candidates = sorted(set(ports if ports is not None else glob.glob(pattern)))
    inspect_one = inspector or inspect_compatibility_port
    port_reports = [] if enumeration_only else [await inspect_one(port) for port in candidates]
    host_facts = host if host is not None else collect_host_facts()
    acm_interfaces = interfaces if interfaces is not None else collect_acm_interfaces()
    usb_devices = group_acm_interfaces(acm_interfaces)
    enumeration = _enumeration_summary(host_facts, usb_devices)
    at_ports = [row for row in port_reports if row.get("at_response")]
    firmwares = sorted({str(row.get("firmware")) for row in at_ports if row.get("firmware")})
    validation_ready = (
        not enumeration_only
        and enumeration["enumeration_ready"]
        and any(row.get("firmware") for row in at_ports)
    )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "compatibility",
        "mode": "enumeration-only" if enumeration_only else "full",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent_version": __version__,
        "redaction": _redaction(),
        "host": host_facts,
        "acm_interfaces": acm_interfaces,
        "usb_devices": usb_devices,
        "ports": port_reports,
        "summary": {
            "candidate_device_nodes": len(candidates),
            "enumerated_acm_interfaces": len(acm_interfaces),
            **enumeration,
            "at_inspection_performed": not enumeration_only,
            "at_ports": len(at_ports),
            "firmwares": firmwares,
            "validation_ready": validation_ready,
        },
    }


def _hotplug_snapshot(interfaces: list[dict[str, Any]], host: dict[str, Any]) -> dict[str, Any]:
    devices = group_acm_interfaces(interfaces)
    air780e_devices = [row for row in devices if row.get("is_air780e")]
    enumeration = _enumeration_summary(host, devices)
    return {
        "air780e_devices": air780e_devices,
        "air780e_usb_devices": enumeration["air780e_usb_devices"],
        "complete_air780e_layouts": enumeration["complete_air780e_layouts"],
        "device_nodes_ready": enumeration["device_nodes_ready"],
        "enumeration_ready": enumeration["enumeration_ready"],
        "modem_manager_safety": enumeration["modem_manager_safety"],
    }


def _device_names(snapshot: dict[str, Any]) -> set[str]:
    return {
        str(interface.get("name") or "")
        for device in snapshot["air780e_devices"]
        for interface in device["interfaces"]
        if interface.get("name")
    }


def _usb_paths(snapshot: dict[str, Any]) -> set[str]:
    return {
        str(device.get("usb_path") or "")
        for device in snapshot["air780e_devices"]
        if device.get("usb_path")
    }


async def observe_hotplug(
    *,
    duration: float,
    poll_interval: float = 0.5,
    collector: InterfaceCollector = collect_acm_interfaces,
    host: dict[str, Any] | None = None,
    sleeper: Sleeper = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Record changed sysfs/udev topology without ever opening a tty."""
    if duration <= 0:
        raise ValueError("duration must be greater than zero")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")

    host_facts = host if host is not None else collect_host_facts()
    started = monotonic()
    deadline = started + duration
    transitions: list[dict[str, Any]] = []
    samples = 0
    previous_fingerprint = ""

    def sample() -> None:
        nonlocal samples, previous_fingerprint
        samples += 1
        snapshot = _hotplug_snapshot(collector(), host_facts)
        fingerprint = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        if fingerprint == previous_fingerprint:
            return
        previous_fingerprint = fingerprint
        transitions.append(
            {
                "elapsed_seconds": round(max(0.0, monotonic() - started), 3),
                **snapshot,
            }
        )

    sample()
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        await sleeper(min(poll_interval, remaining))
        sample()

    initial = transitions[0]
    initial_count = int(initial["air780e_usb_devices"])
    minimum_count = min(int(row["air780e_usb_devices"]) for row in transitions)
    disappearance_index = next(
        (
            index
            for index, row in enumerate(transitions[1:], start=1)
            if int(row["air780e_usb_devices"]) < initial_count
        ),
        None,
    )
    reappeared: dict[str, Any] | None = None
    restored: dict[str, Any] | None = None
    if disappearance_index is not None:
        reappeared = next(
            (
                row
                for row in transitions[disappearance_index + 1 :]
                if int(row["air780e_usb_devices"]) == initial_count
            ),
            None,
        )
        restored = next(
            (
                row
                for row in transitions[disappearance_index + 1 :]
                if int(row["air780e_usb_devices"]) == initial_count
                and int(row["complete_air780e_layouts"]) == initial_count
                and row["device_nodes_ready"] is True
            ),
            None,
        )

    disappearance_observed = disappearance_index is not None
    reappearance_observed = reappeared is not None
    topology_restored = (
        restored is not None
        and initial_count > 0
        and int(initial["complete_air780e_layouts"]) == initial_count
    )
    tty_renumbering_observed = reappeared is not None and _device_names(
        reappeared
    ) != _device_names(initial)
    usb_path_change_observed = reappeared is not None and _usb_paths(reappeared) != _usb_paths(
        initial
    )
    final = transitions[-1]
    hotplug_cycle_complete = (
        disappearance_observed
        and reappearance_observed
        and topology_restored
        and final["enumeration_ready"] is True
        and int(final["air780e_usb_devices"]) == initial_count
    )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "hotplug-observation",
        "mode": "enumeration-only",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent_version": __version__,
        "redaction": _redaction(),
        "host": host_facts,
        "duration_seconds": duration,
        "poll_interval_seconds": poll_interval,
        "transitions": transitions,
        "limitations": [
            "serial ports were not opened",
            "worker identity recovery requires separate Agent log or UI evidence",
        ],
        "summary": {
            "samples": samples,
            "topology_transitions": len(transitions),
            "initial_air780e_usb_devices": initial_count,
            "minimum_air780e_usb_devices": minimum_count,
            "final_air780e_usb_devices": final["air780e_usb_devices"],
            "disappearance_observed": disappearance_observed,
            "reappearance_observed": reappearance_observed,
            "tty_renumbering_observed": tty_renumbering_observed,
            "usb_path_change_observed": usb_path_change_observed,
            "topology_restored": topology_restored,
            "final_enumeration_ready": final["enumeration_ready"],
            "hotplug_cycle_complete": hotplug_cycle_complete,
            "validation_ready": hotplug_cycle_complete,
        },
    }
