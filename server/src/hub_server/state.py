"""Shared application state, assembled once at startup."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .auth import Auth
from .config import Settings
from .db import Database
from .gateway import Gateway
from .notify import Notifier

log = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    db: Database
    auth: Auth
    gateway: Gateway
    notifier: Notifier

    @classmethod
    def build(cls, settings: Settings) -> "AppState":
        db = Database(settings.db_path)
        # Nothing is connected at startup, whatever the last run left behind.
        db.mark_all_agents_disconnected()
        auth = Auth(db, session_ttl_hours=settings.session_ttl_hours)
        auth.purge_expired_sessions()
        # The notifier is what makes a stored SMS reach a phone; the gateway
        # hands every newly ingested inbound message straight to it.
        notifier = Notifier(db, settings)
        gateway = Gateway(
            db,
            settings,
            on_message=notifier.on_message,
            on_task_result=notifier.on_task_result,
        )
        state = cls(
            settings=settings, db=db, auth=auth, gateway=gateway, notifier=notifier
        )
        log.info("data dir %s, timezone %s", settings.data_dir, settings.timezone)
        if not auth.is_configured:
            log.warning(
                "no administrator password set yet — the first visit to the web "
                "UI will ask for one"
            )
        return state

    def close(self) -> None:
        self.db.close()
