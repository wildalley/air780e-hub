"""Shared application state, assembled once at startup."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .alerts import OfflineAlerter
from .auth import Auth
from .config import Settings
from .db import SETTING_MESSAGE_RETENTION_DAYS, Database
from .gateway import Gateway
from .notify import Notifier
from .restore import RestoreController

log = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    db: Database
    auth: Auth
    gateway: Gateway
    notifier: Notifier
    alerter: OfflineAlerter
    started_at: str
    started_monotonic: float
    restore: RestoreController = field(default_factory=RestoreController)
    background_factories: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    background_tasks: list[asyncio.Task] = field(default_factory=list)

    @classmethod
    def build(cls, settings: Settings) -> AppState:
        db = Database(settings.db_path)
        # Nothing is connected at startup, whatever the last run left behind.
        db.mark_all_agents_disconnected()
        db.recover_operations()
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
            on_call=notifier.on_call,
            on_device_change=alerter.note,
        )
        state = cls(
            settings=settings, db=db, auth=auth, gateway=gateway,
            notifier=notifier, alerter=alerter,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            started_monotonic=time.monotonic(),
            restore=RestoreController(),
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

    async def stop_background(self) -> None:
        self.gateway.maintenance = True
        tasks, self.background_tasks = self.background_tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def start_background(self) -> None:
        self.gateway.maintenance = False
        self.background_tasks = [
            asyncio.create_task(factory()) for factory in self.background_factories
        ]
        self.notifier.start()

    def rebuild_services(self) -> None:
        self.auth = Auth(self.db, session_ttl_hours=self.settings.session_ttl_hours)
        self.notifier = Notifier(self.db, self.settings)
        self.alerter = OfflineAlerter(
            self.db, self.notifier, grace=self.settings.offline_alert_grace,
        )
        self.gateway = Gateway(
            self.db, self.settings, on_message=self.notifier.on_message,
            on_task_result=self.notifier.on_task_result, on_call=self.notifier.on_call,
            on_device_change=self.alerter.note,
        )

    def rebuild_gateway(self) -> None:
        """Replace a restore-retired registry without replacing live services."""
        self.gateway = Gateway(
            self.db, self.settings, on_message=self.notifier.on_message,
            on_task_result=self.notifier.on_task_result, on_call=self.notifier.on_call,
            on_device_change=self.alerter.note,
        )

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
