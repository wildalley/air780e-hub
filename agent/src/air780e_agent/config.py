"""Agent configuration.

TOML, read once at startup.  Everything the agent needs to run standalone
lives here — the server link is optional, so a misconfigured or unreachable
server never stops messages being received, stored and keep-alive tasks
being executed.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATHS = (
    Path("/etc/air780e-agent/config.toml"),
    Path("./agent.toml"),
)


class ConfigError(Exception):
    pass


@dataclass
class DeviceConfig:
    name: str
    # Empty means "find me": the agent probes the candidate ports and claims
    # the one whose imei/iccid matches.  Set it to pin a udev symlink instead.
    port: str = ""
    imei: str = ""
    iccid: str = ""
    storage: str = "SM"
    label: str = ""
    baudrate: int = 115200
    delete_after_read: bool = True
    # Warn below this supply voltage, in millivolts.  Per device rather than
    # per agent because the modules need not share a supply: one on a bench PSU
    # and one on a long USB run from a hub have different healthy ranges.
    # 0 means "use the agent's built-in default".
    low_voltage_mv: int = 0

    @property
    def is_pinned(self) -> bool:
        return bool(self.port)


@dataclass
class ServerConfig:
    url: str = ""
    token: str = ""
    verify_tls: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.url)


@dataclass
class AgentConfig:
    agent_id: str = "agent"
    db_path: Path = Path("/var/lib/air780e-agent/agent.db")
    devices: list[DeviceConfig] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)

    # How often to sample +CSQ.  Samples that barely differ are dropped
    # before they reach the wire, so this can stay fairly low.
    status_interval: float = 60.0
    # Backoff ceiling for reopening a port that has gone away.
    reconnect_max_delay: float = 60.0
    # A tty can remain present while the module firmware stops answering.
    # Probe it before every status sample and reopen after repeated failures.
    health_check_timeout: float = 5.0
    health_failure_threshold: int = 3
    # Leave transient network detachments alone, then recover in increasingly
    # disruptive stages (automatic selection, RF cycle, module reset).
    registration_recovery_delay: float = 300.0
    recovery_cooldown: float = 300.0
    recovery_max_attempts_24h: int = 6
    # Hard ceiling on the durable outbound queue.
    max_queued_events: int = 100_000
    # How often the keep-alive scheduler looks for due tasks.  These fire
    # every few weeks, so a coarse tick costs nothing and keeps the agent
    # asleep; jitter is what actually decides the minute.
    scheduler_tick: float = 30.0
    # Wait between keep-alive retries (multiplied by the attempt number).
    scheduler_retry_delay: float = 60.0
    # Where to look for modules whose [[devices]] block gives no explicit
    # port.  Every match is probed; the ones that do not speak AT drop out.
    port_glob: str = "/dev/ttyACM*"
    probe_timeout: float = 3.0
    # Adopt a module that matches no [[devices]] block, under a name derived
    # from its IMEI.  Only modules no configured device is waiting for are
    # eligible, so this never competes with an explicit binding — it means
    # "plug in a new module and it works" rather than "guess between two".
    autodetect: bool = True
    # How often to look for newly plugged modules.  Rescanning is three AT
    # commands per unclaimed port, so this can stay in the tens of seconds.
    autodetect_interval: float = 60.0

    @classmethod
    def load(cls, path: str | Path | None = None) -> AgentConfig:
        candidates = [Path(path)] if path else list(DEFAULT_CONFIG_PATHS)
        for candidate in candidates:
            if candidate.exists():
                return cls.parse(candidate.read_bytes(), source=candidate)
        raise ConfigError(
            "no config file found; looked at "
            + ", ".join(str(c) for c in candidates)
        )

    @classmethod
    def parse(cls, raw: bytes, *, source: Path | None = None) -> AgentConfig:
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{source or 'config'}: {exc}") from exc

        agent = data.get("agent", {})
        config = cls(
            agent_id=agent.get("id", "agent"),
            db_path=Path(agent.get("db", cls.db_path)),
            status_interval=float(agent.get("status_interval", 60.0)),
            reconnect_max_delay=float(agent.get("reconnect_max_delay", 60.0)),
            health_check_timeout=float(agent.get("health_check_timeout", 5.0)),
            health_failure_threshold=int(agent.get("health_failure_threshold", 3)),
            registration_recovery_delay=float(
                agent.get("registration_recovery_delay", 300.0)
            ),
            recovery_cooldown=float(agent.get("recovery_cooldown", 300.0)),
            recovery_max_attempts_24h=int(
                agent.get("recovery_max_attempts_24h", 6)
            ),
            max_queued_events=int(agent.get("max_queued_events", 100_000)),
            scheduler_tick=float(agent.get("scheduler_tick", 30.0)),
            scheduler_retry_delay=float(agent.get("scheduler_retry_delay", 60.0)),
            port_glob=str(agent.get("port_glob", "/dev/ttyACM*")),
            probe_timeout=float(agent.get("probe_timeout", 3.0)),
            autodetect=bool(agent.get("autodetect", True)),
            autodetect_interval=float(agent.get("autodetect_interval", 60.0)),
        )

        server = data.get("server", {})
        config.server = ServerConfig(
            url=server.get("url", ""),
            token=server.get("token", ""),
            verify_tls=bool(server.get("verify_tls", True)),
        )

        devices = data.get("devices", [])
        if not devices and not config.autodetect:
            raise ConfigError(
                "no [[devices]] configured and autodetect is off — nothing to do"
            )

        seen: set[str] = set()
        for entry in devices:
            if "name" not in entry:
                raise ConfigError("each [[devices]] needs a name")
            name = str(entry["name"])
            if name in seen:
                raise ConfigError(f"duplicate device name {name!r}")
            seen.add(name)

            port = str(entry.get("port", "")).strip()
            imei = str(entry.get("imei", "")).strip()
            iccid = str(entry.get("iccid", "")).strip()
            # One module with nothing to go on is unambiguous — there is only
            # one thing it could be.  Two are not, and picking by enumeration
            # order would silently swap the cards after a replug.
            if not (port or imei or iccid) and len(devices) > 1:
                raise ConfigError(
                    f"device {name!r} needs port, imei or iccid — with more "
                    "than one module the agent cannot tell them apart"
                )

            config.devices.append(
                DeviceConfig(
                    name=name,
                    port=port,
                    imei=imei,
                    iccid=iccid,
                    storage=str(entry.get("storage", "SM")),
                    label=str(entry.get("label", "")),
                    baudrate=int(entry.get("baudrate", 115200)),
                    delete_after_read=bool(entry.get("delete_after_read", True)),
                    low_voltage_mv=int(entry.get("low_voltage_mv", 0)),
                )
            )

        if config.server.enabled and not config.server.token:
            raise ConfigError("server.url is set but server.token is empty")

        return config


EXAMPLE_CONFIG = """\
# air780e-agent configuration

[agent]
id = "site-a"
db = "/var/lib/air780e-agent/agent.db"
status_interval = 60.0
# Self-healing defaults are conservative.  Set recovery_max_attempts_24h = 0
# to disable automatic network recovery while retaining serial reconnects.
health_check_timeout = 5.0
health_failure_threshold = 3
registration_recovery_delay = 300.0
recovery_cooldown = 300.0
recovery_max_attempts_24h = 6
# Adopt a module that no [[devices]] block below matches, under a name derived
# from its IMEI (auto-372050).  A brand new module works as soon as it is
# plugged in; the name is remembered, so it stays the same across restarts.
# Set to false to accept only the modules named below.
autodetect = true
autodetect_interval = 60.0

[server]
# Leave url empty to run fully standalone (messages still stored locally,
# keep-alive tasks still executed).
url = "wss://sms.example.com/ws"
token = "change-me"

# One block per module.  The agent finds each one by asking the modules who
# they are (ATI / AT+CGSN / AT+ICCID) and claiming the port whose identity
# matches — so it does not matter which USB socket a module is in, or how
# /dev/ttyACM* happens to be numbered after a reboot.
#
# Blocks are optional when autodetect is on.  Name a module here to give it a
# stable name and a label, or to hold its name while it is unplugged.
#
# Find the values with:  python -m air780e_agent.probe --scan
#
#   imei   identifies the module   — survives swapping the SIM card
#   iccid  identifies the card     — survives swapping the module
#   port   pins a path outright    — for a udev symlink; skips discovery
#
# Give both imei and iccid to require "this card, in this module".
[[devices]]
name = "modem-a"
label = "SIM A"
imei = "000000000000001"
# Warn below this supply voltage in millivolts; omit to use the default.
# low_voltage_mv = 3500

[[devices]]
name = "modem-b"
label = "SIM B"
imei = "000000000000002"
"""
