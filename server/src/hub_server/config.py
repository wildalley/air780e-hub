"""Server settings loaded from environment variables.

Environment-based configuration keeps runtime secrets outside the image and
works consistently with Docker, systemd, and container management panels.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8080

    # Shared secret the agent presents on /ws.  Generated on first start if
    # unset, and written next to the database so it survives restarts.
    agent_token: str = ""
    agent_token_from_env: bool = False

    session_ttl_hours: int = 24 * 14
    # Default SMS retention, in days; 0 disables deletion.  The operator can
    # override it on the Notify page, where the settings table stores the
    # effective value (see AppState.message_retention_days).
    message_retention_days: int = 90
    status_retention_days: int = 30

    # Push retries *per channel*, on top of the first attempt.  A phone that
    # missed a verification code is the failure mode worth spending time on.
    notify_retries: int = 2
    notify_timeout: float = 10.0

    # How long a module must stay offline before it is paged, in seconds.  Long
    # enough to ride out a USB re-enumeration or a broadband blip that drops the
    # agent's link; a module back within the window is never announced.
    offline_alert_grace: float = 120.0

    timezone: str = "Asia/Shanghai"
    # Trust X-Forwarded-* headers when running behind a trusted reverse proxy.
    behind_proxy: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "agent_token"

    @classmethod
    def from_env(cls) -> "Settings":
        configured_token = os.environ.get("HUB_AGENT_TOKEN", "").strip()
        settings = cls(
            data_dir=Path(os.environ.get("HUB_DATA_DIR", "/data")),
            host=os.environ.get("HUB_HOST", "0.0.0.0"),
            port=int(os.environ.get("HUB_PORT", "8080")),
            agent_token=configured_token,
            agent_token_from_env=bool(configured_token),
            session_ttl_hours=int(os.environ.get("HUB_SESSION_TTL_HOURS", 24 * 14)),
            message_retention_days=int(
                os.environ.get("HUB_MESSAGE_RETENTION_DAYS", "90")
            ),
            status_retention_days=int(
                os.environ.get("HUB_STATUS_RETENTION_DAYS", "30")
            ),
            notify_retries=int(os.environ.get("HUB_NOTIFY_RETRIES", "2")),
            notify_timeout=float(os.environ.get("HUB_NOTIFY_TIMEOUT", "10")),
            offline_alert_grace=float(
                os.environ.get("HUB_OFFLINE_ALERT_GRACE", "120")
            ),
            timezone=os.environ.get("HUB_TZ", "Asia/Shanghai"),
            behind_proxy=_bool("HUB_BEHIND_PROXY", True),
        )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.ensure_agent_token()
        return settings

    def ensure_agent_token(self) -> str:
        """Return the agent token, generating and persisting one if needed."""
        if self.agent_token:
            return self.agent_token
        if self.token_path.exists():
            self.agent_token = self.token_path.read_text().strip()
            if self.agent_token:
                return self.agent_token

        self.agent_token = secrets.token_urlsafe(32)
        self.token_path.write_text(self.agent_token + "\n")
        self.token_path.chmod(0o600)
        return self.agent_token

    def replace_agent_token(self, token: str) -> None:
        """Persist a generated replacement without a partially written file."""
        if self.agent_token_from_env:
            raise ConfigError(
                "HUB_AGENT_TOKEN controls this deployment; rotate it in the "
                "deployment environment and restart the server"
            )
        temporary = self.token_path.with_name(f".{self.token_path.name}.tmp")
        try:
            temporary.write_text(token + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.token_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self.agent_token = token
