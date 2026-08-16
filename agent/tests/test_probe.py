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
    usb = tmp_path / "sys" / "devices" / "usb1" / "1-3"
    interface = usb / "1-3:1.2"
    tty = sysfs_root / "ttyACM0"
    interface.mkdir(parents=True)
    tty.mkdir(parents=True)
    device_root.mkdir()

    (usb / "idVendor").write_text("19d1\n")
    (usb / "idProduct").write_text("0001\n")
    (usb / "manufacturer").write_text("EigenComm\n")
    (usb / "product").write_text("EigenComm Compo\n")
    (usb / "serial").write_text("000000000001\n")
    (interface / "bInterfaceNumber").write_text("02\n")
    (tty / "device").symlink_to(interface, target_is_directory=True)
    (device_root / "ttyACM0").touch()
    (device_root / "ttyACM0").chmod(0o660)
    monkeypatch.setattr(
        compat.grp,
        "getgrgid",
        lambda _gid: SimpleNamespace(gr_name="private-local-group"),
    )

    rows = compat.collect_acm_interfaces(
        sysfs_root=sysfs_root,
        device_root=device_root,
    )

    assert len(rows) == 1
    assert rows[0]["usb_path"] == "1-3"
    assert rows[0]["usb_vid"] == "19d1"
    assert rows[0]["usb_pid"] == "0001"
    assert rows[0]["interface_number"] == "02"
    assert rows[0]["device_node_present"] is True
    assert rows[0]["device_mode"] == "0660"
    assert rows[0]["device_group"] == "other"
    assert rows[0]["usb_serial_is_generic_air780e"] is True
    rendered = json.dumps(rows)
    assert "000000000001" not in rendered
    assert "private-local-group" not in rendered


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
        host={"os": "test"},
        interfaces=[
            {"name": "ttyACM0", "usb_path": "1-3"},
            {"name": "ttyACM1", "usb_path": "1-3"},
            {"name": "ttyACM2", "usb_path": "1-3"},
        ],
    )

    assert report["host"] == {"os": "test"}
    assert report["summary"] == {
        "candidate_device_nodes": 3,
        "enumerated_acm_interfaces": 3,
        "usb_devices": 1,
        "at_ports": 2,
        "firmwares": ["V1011"],
        "validation_ready": True,
    }
    assert report["redaction"]["shareable_by_default"] is True

    incomplete = await compat.build_compatibility_report(
        ports=["/dev/ttyACM2"],
        inspector=inspector,
        host={"os": "test"},
        interfaces=[],
    )
    assert incomplete["summary"]["validation_ready"] is False


def _report(*, ready: bool) -> dict:
    return {
        "schema_version": 1,
        "summary": {
            "at_ports": 1 if ready else 0,
            "usb_devices": 1,
            "validation_ready": ready,
        },
    }


def test_report_cli_writes_json_and_returns_validation_status(
    tmp_path, monkeypatch, capsys
):
    async def report_builder(**_kwargs):
        return _report(ready=True)

    monkeypatch.setattr(probe, "build_compatibility_report", report_builder)
    output = tmp_path / "compat.json"

    assert probe.main(["--report", str(output)]) == 0
    assert json.loads(output.read_text()) == _report(ready=True)
    assert "1 AT port(s), 1 USB device(s)" in capsys.readouterr().out


def test_report_cli_keeps_incomplete_evidence_but_returns_failure(
    tmp_path, monkeypatch
):
    async def report_builder(**_kwargs):
        return _report(ready=False)

    monkeypatch.setattr(probe, "build_compatibility_report", report_builder)
    output = tmp_path / "incomplete.json"

    assert probe.main(["--report", str(output)]) == 1
    assert output.exists(), "the incomplete report explains why validation failed"
