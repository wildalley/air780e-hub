"""Compatibility reports are useful evidence only when they are safe to share."""

from __future__ import annotations

import json
from types import SimpleNamespace

from air780e_agent import compat, probe
from air780e_agent.at import PipeTransport
from air780e_agent.mock import MockAir780E


async def test_port_report_is_read_only_and_omits_identifiers():
    agent_side, modem_side = PipeTransport.create_pair()
    mock = MockAir780E(
        transport=modem_side,
        manufacturer="EigenComm",
        hardware_model="Air780EPV",
        firmware="AirM2M_780EPV_V1011_LTE_AT",
        imei="867567048825490",
        iccid="89860622180012345670",
        smsc="+8613800210500",
    )
    await mock.start()
    mock.fill_storage(1)
    try:
        report = await compat.inspect_compatibility_port(
            "/dev/fake-at",
            transport_factory=lambda _port: agent_side,
        )
    finally:
        await mock.stop()

    assert report["open"] is True
    assert report["at_response"] is True
    assert report["manufacturer"] == "EigenComm"
    assert report["model"] == "Air780EPV"
    assert report["firmware"] == "AirM2M_780EPV_V1011_LTE_AT"
    assert report["imei_available"] is True
    assert report["iccid_available"] is True
    assert report["smsc_configured"] is True
    assert report["storage"] == {"name": "SM", "used": 1, "capacity": 10}
    assert mock.stored_count == 1, "collecting evidence must not drain the inbox"
    assert not any(command.startswith("AT+CMG") for command in mock.commands)

    rendered = json.dumps(report)
    assert "867567048825490" not in rendered
    assert "89860622180012345670" not in rendered
    assert "+8613800210500" not in rendered


async def test_unsupported_firmware_query_is_visible_not_guessed():
    agent_side, modem_side = PipeTransport.create_pair()
    mock = MockAir780E(transport=modem_side, unsupported={"AT+CGMR"})
    await mock.start()
    try:
        report = await compat.inspect_compatibility_port(
            "/dev/fake-at",
            transport_factory=lambda _port: agent_side,
        )
    finally:
        await mock.stop()

    assert report["at_response"] is True
    assert report["firmware"] == ""
    assert report["commands"]["AT+CGMR"] == "CmeError"


def test_modem_manager_service_state_is_recorded(monkeypatch):
    assert compat._modem_manager_state(False) == "not-installed"
    monkeypatch.setattr(compat.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(
        compat.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="active\n"),
    )
    assert compat._modem_manager_state(True) == "active"


def test_sysfs_interfaces_are_grouped_without_local_identifiers(tmp_path, monkeypatch):
    sysfs_root = tmp_path / "sys" / "class" / "tty"
    device_root = tmp_path / "dev"
    udev_data_root = tmp_path / "run" / "udev" / "data"
    usb = tmp_path / "sys" / "devices" / "usb1" / "1-3"
    interface = usb / "1-3:1.2"
    tty = sysfs_root / "ttyACM0"
    interface.mkdir(parents=True)
    tty.mkdir(parents=True)
    device_root.mkdir()
    udev_data_root.mkdir(parents=True)

    (usb / "idVendor").write_text("19d1\n")
    (usb / "idProduct").write_text("0001\n")
    (usb / "manufacturer").write_text("EigenComm\n")
    (usb / "product").write_text("EigenComm Compo\n")
    (usb / "serial").write_text("000000000001\n")
    (usb / "dev").write_text("189:1\n")
    (interface / "bInterfaceNumber").write_text("02\n")
    (tty / "dev").write_text("166:0\n")
    (tty / "device").symlink_to(interface, target_is_directory=True)
    (device_root / "ttyACM0").touch()
    (device_root / "ttyACM0").chmod(0o660)
    (udev_data_root / "c189:1").write_text("E:ID_MM_DEVICE_IGNORE=1\n")
    (udev_data_root / "c166:0").write_text("E:ID_MM_PORT_IGNORE=1\n")
    monkeypatch.setattr(
        compat.grp,
        "getgrgid",
        lambda _gid: SimpleNamespace(gr_name="private-local-group"),
    )

    rows = compat.collect_acm_interfaces(
        sysfs_root=sysfs_root,
        device_root=device_root,
        udev_data_root=udev_data_root,
    )

    assert len(rows) == 1
    assert rows[0]["usb_path"] == "1-3"
    assert rows[0]["usb_vid"] == "19d1"
    assert rows[0]["usb_pid"] == "0001"
    assert rows[0]["interface_number"] == "02"
    assert rows[0]["device_node_present"] is True
    assert rows[0]["device_node_readable"] is True
    assert rows[0]["device_node_writable"] is True
    assert rows[0]["device_node_ready"] is True
    assert rows[0]["device_mode"] == "0660"
    assert rows[0]["device_group"] == "other"
    assert rows[0]["usb_serial_is_generic_air780e"] is True
    assert rows[0]["modem_manager_device_ignore"] is True
    assert rows[0]["modem_manager_port_ignore"] is True
    rendered = json.dumps(rows)
    assert "000000000001" not in rendered
    assert "private-local-group" not in rendered


def _air780e_interfaces(
    usb_path: str,
    names: tuple[str, ...],
    *,
    numbers: tuple[str, ...] = compat.AIR780E_ACM_INTERFACES,
    present: bool = True,
    ready: bool | None = None,
    ignored: bool | None = True,
) -> list[dict]:
    node_ready = present if ready is None else ready
    return [
        {
            "name": name,
            "device_node": f"/dev/{name}",
            "device_node_present": present,
            "device_node_readable": node_ready,
            "device_node_writable": node_ready,
            "device_node_ready": node_ready,
            "interface_number": number,
            "usb_path": usb_path,
            "usb_vid": compat.AIR780E_USB_VID,
            "usb_pid": compat.AIR780E_USB_PID,
            "modem_manager_device_ignore": ignored,
            "modem_manager_port_ignore": ignored,
        }
        for name, number in zip(names, numbers, strict=True)
    ]


async def test_report_summary_requires_a_real_firmware_read():
    async def inspector(port: str) -> dict:
        return {
            "port": port,
            "at_response": not port.endswith("0"),
            "firmware": "V1011" if port.endswith("1") else "",
        }

    report = await compat.build_compatibility_report(
        ports=["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2"],
        inspector=inspector,
        host={
            "os": "test",
            "cdc_acm_loaded": True,
            "modem_manager_cli_installed": True,
            "modem_manager_service": "active",
        },
        interfaces=_air780e_interfaces("1-3", ("ttyACM0", "ttyACM1", "ttyACM2")),
    )

    assert report["schema_version"] == 2
    assert report["mode"] == "full"
    assert report["summary"]["candidate_device_nodes"] == 3
    assert report["summary"]["enumerated_acm_interfaces"] == 3
    assert report["summary"]["usb_devices"] == 1
    assert report["summary"]["air780e_usb_devices"] == 1
    assert report["summary"]["complete_air780e_layouts"] == 1
    assert report["summary"]["device_nodes_ready"] is True
    assert report["summary"]["enumeration_ready"] is True
    assert report["summary"]["modem_manager_safety"] == "ignore-applied"
    assert report["summary"]["at_ports"] == 2
    assert report["summary"]["firmwares"] == ["V1011"]
    assert report["summary"]["validation_ready"] is True
    assert report["usb_devices"][0]["interface_numbers"] == ["02", "04", "06"]
    assert report["usb_devices"][0]["complete_acm_layout"] is True
    assert report["redaction"]["shareable_by_default"] is True

    incomplete = await compat.build_compatibility_report(
        ports=["/dev/ttyACM2"],
        inspector=inspector,
        host={"os": "test", "cdc_acm_loaded": True},
        interfaces=_air780e_interfaces(
            "1-3",
            ("ttyACM0", "ttyACM2"),
            numbers=("02", "06"),
        ),
    )
    assert incomplete["summary"]["enumeration_ready"] is False
    assert incomplete["summary"]["validation_ready"] is False

    permission_denied = await compat.build_compatibility_report(
        host={"cdc_acm_loaded": True},
        interfaces=_air780e_interfaces(
            "1-3",
            ("ttyACM0", "ttyACM1", "ttyACM2"),
            ready=False,
        ),
        enumeration_only=True,
    )
    assert permission_denied["summary"]["device_nodes_ready"] is False
    assert permission_denied["summary"]["enumeration_ready"] is False

    unprotected = await compat.build_compatibility_report(
        host={
            "cdc_acm_loaded": True,
            "modem_manager_cli_installed": True,
            "modem_manager_service": "active",
        },
        interfaces=_air780e_interfaces(
            "1-3",
            ("ttyACM0", "ttyACM1", "ttyACM2"),
            ignored=False,
        ),
        enumeration_only=True,
    )
    assert unprotected["summary"]["modem_manager_safety"] == "unprotected"


async def test_enumeration_only_never_calls_the_at_inspector():
    async def inspector(_port: str) -> dict:
        raise AssertionError("enumeration-only reporting must not open a tty")

    report = await compat.build_compatibility_report(
        ports=["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2"],
        inspector=inspector,
        host={
            "cdc_acm_loaded": True,
            "modem_manager_cli_installed": False,
        },
        interfaces=_air780e_interfaces("1-3", ("ttyACM0", "ttyACM1", "ttyACM2")),
        enumeration_only=True,
    )

    assert report["mode"] == "enumeration-only"
    assert report["ports"] == []
    assert report["summary"]["at_inspection_performed"] is False
    assert report["summary"]["enumeration_ready"] is True
    assert report["summary"]["validation_ready"] is False
    assert report["summary"]["modem_manager_safety"] == "not-installed"


async def test_hotplug_observer_records_loss_recovery_and_renumbering():
    initial = _air780e_interfaces("1-3", ("ttyACM0", "ttyACM1", "ttyACM2")) + _air780e_interfaces(
        "3-2", ("ttyACM3", "ttyACM4", "ttyACM5")
    )
    one_missing = _air780e_interfaces("3-2", ("ttyACM3", "ttyACM4", "ttyACM5"))
    restored = _air780e_interfaces("1-4", ("ttyACM6", "ttyACM7", "ttyACM8")) + _air780e_interfaces(
        "3-2", ("ttyACM3", "ttyACM4", "ttyACM5")
    )
    snapshots = [initial, one_missing, restored, restored]
    clock = 0.0
    sample_index = 0

    def collect() -> list[dict]:
        nonlocal sample_index
        value = snapshots[min(sample_index, len(snapshots) - 1)]
        sample_index += 1
        return value

    async def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    report = await compat.observe_hotplug(
        duration=3,
        poll_interval=1,
        collector=collect,
        host={
            "cdc_acm_loaded": True,
            "modem_manager_cli_installed": False,
        },
        sleeper=sleep,
        monotonic=lambda: clock,
    )

    assert report["report_type"] == "hotplug-observation"
    assert len(report["transitions"]) == 3
    assert report["summary"]["samples"] == 4
    assert report["summary"]["initial_air780e_usb_devices"] == 2
    assert report["summary"]["minimum_air780e_usb_devices"] == 1
    assert report["summary"]["disappearance_observed"] is True
    assert report["summary"]["reappearance_observed"] is True
    assert report["summary"]["tty_renumbering_observed"] is True
    assert report["summary"]["usb_path_change_observed"] is True
    assert report["summary"]["topology_restored"] is True
    assert report["summary"]["final_enumeration_ready"] is True
    assert report["summary"]["hotplug_cycle_complete"] is True
    assert "identity recovery" in report["limitations"][1]


def _report(*, ready: bool) -> dict:
    return {
        "schema_version": 2,
        "report_type": "compatibility",
        "summary": {
            "at_ports": 1 if ready else 0,
            "usb_devices": 1,
            "air780e_usb_devices": 1,
            "complete_air780e_layouts": 1,
            "enumeration_ready": ready,
            "validation_ready": ready,
        },
    }


def test_report_cli_writes_json_and_returns_validation_status(tmp_path, monkeypatch, capsys):
    async def report_builder(**_kwargs):
        return _report(ready=True)

    monkeypatch.setattr(probe, "build_compatibility_report", report_builder)
    output = tmp_path / "compat.json"

    assert probe.main(["--report", str(output)]) == 0
    assert json.loads(output.read_text()) == _report(ready=True)
    assert "1 AT port(s), 1 Air780E USB device(s)" in capsys.readouterr().out


def test_report_cli_keeps_incomplete_evidence_but_returns_failure(tmp_path, monkeypatch):
    async def report_builder(**_kwargs):
        return _report(ready=False)

    monkeypatch.setattr(probe, "build_compatibility_report", report_builder)
    output = tmp_path / "incomplete.json"

    assert probe.main(["--report", str(output)]) == 1
    assert output.exists(), "the incomplete report explains why validation failed"


def test_enumeration_report_cli_uses_enumeration_status(tmp_path, monkeypatch, capsys):
    called: dict = {}

    async def report_builder(**kwargs):
        called.update(kwargs)
        return _report(ready=True)

    monkeypatch.setattr(probe, "build_compatibility_report", report_builder)
    output = tmp_path / "enumeration.json"

    assert probe.main(["--report", str(output), "--enumeration-only"]) == 0
    assert called["enumeration_only"] is True
    assert "1 Air780E USB device(s), 1 complete layout(s)" in capsys.readouterr().out


def test_hotplug_cli_persists_incomplete_observation(tmp_path, monkeypatch):
    report = {
        "schema_version": 2,
        "report_type": "hotplug-observation",
        "summary": {
            "topology_transitions": 1,
            "hotplug_cycle_complete": False,
            "validation_ready": False,
        },
    }

    async def observer(**_kwargs):
        return report

    monkeypatch.setattr(probe, "observe_hotplug", observer)
    output = tmp_path / "hotplug.json"

    assert probe.main(["--report", str(output), "--observe-hotplug", "10"]) == 1
    assert json.loads(output.read_text()) == report
