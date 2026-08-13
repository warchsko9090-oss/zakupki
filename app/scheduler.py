from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings

log = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None
_last_poll: dict[str, Any] = {
    "at": None,
    "ok": None,
    "checked": 0,
    "saved": 0,
    "error": None,
}


def get_last_poll() -> dict[str, Any]:
    return dict(_last_poll)


def start_scheduler() -> None:
    global _scheduler
    get_settings.cache_clear()
    settings = get_settings()
    if _scheduler is not None:
        return
    if not settings.mail_poll_enabled:
        log.info("Mail poll scheduler skipped (MAIL_POLL_ENABLED=false)")
        return
    if not settings.mail_ready:
        log.info("Mail poll scheduler skipped (mail not ready)")
        return

    _scheduler = BackgroundScheduler(timezone=settings.timezone)
    minutes = max(2, int(settings.mail_poll_minutes or 3))
    _scheduler.add_job(
        _job_poll_replies,
        IntervalTrigger(minutes=minutes),
        id="poll_quote_replies",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # First check soon after start (don't wait a full interval)
    _scheduler.add_job(
        _job_poll_replies,
        "date",
        run_date=datetime.now(timezone.utc),
        id="poll_quote_replies_startup",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("Mail reply auto-poll every %s min (%s)", minutes, settings.timezone)


def _job_poll_replies() -> None:
    global _last_poll
    try:
        from app.mail import poll_replies

        # UNSEEN only — safe for background; manual button can force recent window
        result = poll_replies(force_seen=False, max_messages=40, since_days=14)
        _last_poll = {
            "at": datetime.now(timezone.utc).isoformat(),
            "ok": bool(result.get("ok")),
            "checked": int(result.get("checked") or 0),
            "saved": int(result.get("saved") or 0),
            "error": result.get("error"),
        }
        log.info(
            "Auto-poll: ok=%s checked=%s saved=%s err=%s",
            _last_poll["ok"],
            _last_poll["checked"],
            _last_poll["saved"],
            _last_poll["error"],
        )
    except Exception as exc:
        _last_poll = {
            "at": datetime.now(timezone.utc).isoformat(),
            "ok": False,
            "checked": 0,
            "saved": 0,
            "error": str(exc),
        }
        log.exception("Poll replies job failed")
