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
    port: str
    storage: str = "SM"
    label: str = ""
    baudrate: int = 115200
    delete_after_read: bool = True


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
            if "name" not in entry or "port" not in entry:
                raise ConfigError("each [[devices]] needs both name and port")
            name = str(entry["name"])
            if name in seen:
                raise ConfigError(f"duplicate device name {name!r}")
            seen.add(name)
            config.devices.append(
                DeviceConfig(
                    name=name,
                    port=str(entry["port"]),
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
id = "home-arch"
db = "/var/lib/air780e-agent/agent.db"
status_interval = 60.0

[server]
# Leave url empty to run fully standalone (messages still stored locally,
# keep-alive tasks still executed).
url = "wss://sms.example.com/ws"
token = "change-me"

# One block per module.  Bind these paths with udev rules by USB port path —
# two identical modules usually report the same serial number, so by-id will
# collide.  See docs/at-reference.md section 3.3.
[[devices]]
name = "a"
label = "移动卡"
port = "/dev/air780e-a"
storage = "SM"

[[devices]]
name = "b"
label = "联通卡"
port = "/dev/air780e-b"
storage = "SM"
"""
