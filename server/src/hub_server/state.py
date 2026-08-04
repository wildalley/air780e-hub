"""Shared application state, assembled once at startup."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .auth import Auth
from .alerts import OfflineAlerter
from .config import Settings
from .db import Database, SETTING_MESSAGE_RETENTION_DAYS
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
    alerter: OfflineAlerter

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
        # The alerter turns a module dropping off (a status edge, or a whole
        # agent disconnecting) into a debounced push through the same notifier.
        alerter = OfflineAlerter(db, notifier, grace=settings.offline_alert_grace)
        gateway = Gateway(
            db,
            settings,
            on_message=notifier.on_message,
            on_task_result=notifier.on_task_result,
            on_device_change=alerter.note,
        )
        state = cls(
            settings=settings, db=db, auth=auth, gateway=gateway,
            notifier=notifier, alerter=alerter,
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

    @property
    def message_retention_days(self) -> int:
        """Effective SMS retention window, in days.

        The operator's saved value on the Notify page wins; absent that, the
        environment default (``Settings.message_retention_days``) applies.  One
        accessor so housekeeping, the manual purge and the settings API can
        never disagree on which number is in force.  0 means "keep forever".
        """
        stored = self.db.get_setting(
            SETTING_MESSAGE_RETENTION_DAYS, self.settings.message_retention_days
        )
        try:
            return int(stored)
        except (TypeError, ValueError):
            return self.settings.message_retention_days
