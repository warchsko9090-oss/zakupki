from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings

log = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    settings = get_settings()
    if _scheduler is not None:
        return
    if not settings.mail_poll_enabled or not settings.mail_ready:
        log.info("Mail poll scheduler skipped (disabled or mail not ready)")
        return

    _scheduler = BackgroundScheduler(timezone=settings.timezone)
    minutes = max(2, int(settings.mail_poll_minutes or 10))
    _scheduler.add_job(
        _job_poll_replies,
        IntervalTrigger(minutes=minutes),
        id="poll_quote_replies",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("Mail reply poll every %s min (%s)", minutes, settings.timezone)


def _job_poll_replies() -> None:
    try:
        from app.mail import poll_replies

        result = poll_replies()
        log.info(
            "Poll replies: ok=%s checked=%s saved=%s",
            result.get("ok"),
            result.get("checked"),
            result.get("saved"),
        )
    except Exception:
        log.exception("Poll replies job failed")
