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

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AgentConfig":
        candidates = [Path(path)] if path else list(DEFAULT_CONFIG_PATHS)
        for candidate in candidates:
            if candidate.exists():
                return cls.parse(candidate.read_bytes(), source=candidate)
        raise ConfigError(
            "no config file found; looked at "
            + ", ".join(str(c) for c in candidates)
        )

    @classmethod
    def parse(cls, raw: bytes, *, source: Path | None = None) -> "AgentConfig":
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
            max_queued_events=int(agent.get("max_queued_events", 100_000)),
            scheduler_tick=float(agent.get("scheduler_tick", 30.0)),
            scheduler_retry_delay=float(agent.get("scheduler_retry_delay", 60.0)),
            port_glob=str(agent.get("port_glob", "/dev/ttyACM*")),
            probe_timeout=float(agent.get("probe_timeout", 3.0)),
        )

        server = data.get("server", {})
        config.server = ServerConfig(
            url=server.get("url", ""),
            token=server.get("token", ""),
            verify_tls=bool(server.get("verify_tls", True)),
        )

        devices = data.get("devices", [])
        if not devices:
            raise ConfigError("no [[devices]] configured — nothing to do")

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

[[devices]]
name = "modem-b"
label = "SIM B"
imei = "000000000000002"
"""
