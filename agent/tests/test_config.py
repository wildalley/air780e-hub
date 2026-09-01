"""Config parsing."""

from __future__ import annotations

import pytest

from air780e_agent.config import AgentConfig, ConfigError

MINIMAL = b"""
[[devices]]
name = "a"
port = "/dev/air780e-a"
"""

FULL = b"""
[agent]
id = "home-arch"
db = "/tmp/agent.db"
status_interval = 30.0
health_check_timeout = 2.5
health_failure_threshold = 4
registration_recovery_delay = 120.0
recovery_cooldown = 180.0
recovery_max_attempts_24h = 5

[server]
url = "wss://sms.example.com/ws"
token = "secret"

[[devices]]
name = "a"
label = "\xe7\xa7\xbb\xe5\x8a\xa8\xe5\x8d\xa1"
port = "/dev/air780e-a"
storage = "ME"

[[devices]]
name = "b"
port = "/dev/air780e-b"
"""


def test_minimal_config_runs_standalone():
    config = AgentConfig.parse(MINIMAL)
    assert len(config.devices) == 1
    assert config.devices[0].name == "a"
    assert config.server.enabled is False, "no server means standalone, not an error"


def test_full_config():
    config = AgentConfig.parse(FULL)
    assert config.agent_id == "home-arch"
    assert config.status_interval == 30.0
    assert config.health_check_timeout == 2.5
    assert config.health_failure_threshold == 4
    assert config.registration_recovery_delay == 120.0
    assert config.recovery_cooldown == 180.0
    assert config.recovery_max_attempts_24h == 5
    assert config.server.enabled is True
    assert config.server.token == "secret"
    assert [d.name for d in config.devices] == ["a", "b"]
    assert config.devices[0].label == "移动卡"
    assert config.devices[0].storage == "ME"
    assert config.devices[1].storage == "SM"  # default


def test_no_devices_is_fine_when_autodetect_can_find_them():
    """An empty config is a legitimate starting point: plug a module in and it
    gets adopted, with nothing declared in advance."""
    config = AgentConfig.parse(b"[agent]\nid = 'x'\n")
    assert config.devices == []
    assert config.autodetect is True


def test_devices_are_required_when_autodetect_is_off():
    with pytest.raises(ConfigError, match="no \\[\\[devices\\]\\]"):
        AgentConfig.parse(b"[agent]\nid = 'x'\nautodetect = false\n")


def test_autodetect_settings_are_read():
    config = AgentConfig.parse(
        b"[agent]\nautodetect = false\nautodetect_interval = 15.0\n"
        b'[[devices]]\nname = "a"\n'
    )
    assert config.autodetect is False
    assert config.autodetect_interval == 15.0


def test_duplicate_device_names_rejected():
    raw = b"""
[[devices]]
name = "a"
port = "/dev/one"

[[devices]]
name = "a"
port = "/dev/two"
"""
    with pytest.raises(ConfigError, match="duplicate device name"):
        AgentConfig.parse(raw)


def test_device_needs_a_name():
    with pytest.raises(ConfigError, match="needs a name"):
        AgentConfig.parse(b'[[devices]]\nport = "/dev/one"\n')


def test_a_lone_device_needs_no_identity():
    """One module and nothing to go on is unambiguous — there is only one
    thing it could be, so discovery is allowed to take it."""
    config = AgentConfig.parse(b'[[devices]]\nname = "a"\n')
    assert config.devices[0].port == ""
    assert config.devices[0].is_pinned is False


def test_two_devices_must_each_be_identifiable():
    raw = b"""
[[devices]]
name = "a"
imei = "863304089655700"

[[devices]]
name = "b"
"""
    with pytest.raises(ConfigError, match="port, imei or iccid"):
        AgentConfig.parse(raw)


def test_identity_fields_are_read():
    raw = b"""
[[devices]]
name = "a"
imei = "863304089655700"
iccid = "8964052040035110800F"
"""
    device = AgentConfig.parse(raw).devices[0]
    assert device.imei == "863304089655700"
    assert device.iccid == "8964052040035110800F"


def test_server_url_without_token_rejected():
    raw = b"""
[server]
url = "wss://example.com/ws"

[[devices]]
name = "a"
port = "/dev/one"
"""
    with pytest.raises(ConfigError, match="token is empty"):
        AgentConfig.parse(raw)


def test_bad_toml_is_reported_clearly():
    with pytest.raises(ConfigError):
        AgentConfig.parse(b"this is not toml [[[")


def test_example_config_is_valid():
    from air780e_agent.config import EXAMPLE_CONFIG

    config = AgentConfig.parse(EXAMPLE_CONFIG.encode())
    assert [d.name for d in config.devices] == ["modem-a", "modem-b"]
