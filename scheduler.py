"""Расписание утреннего прогона (APScheduler, cron из .env RUN_CRON)."""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronExpressionTrigger

from config import settings

log = logging.getLogger("scheduler")


def _local_tz():
    """Местный tz как объект (не строка 'local' — APScheduler не умеет её
    резолвить на Windows Python, где нет tzdata для 'local')."""
    try:
        return datetime.now().astimezone().tzinfo
    except Exception:  # noqa: BLE001
        return None


def start_scheduler(job) -> AsyncIOScheduler:
    """Starts scheduler with daily job (full cron expression from RUN_CRON)."""
    expr = settings.run_cron
    try:
        trigger = CronExpressionTrigger.from_cron(expr)
    except (TypeError, ValueError):
        log.warning("unsupported RUN_CRON: %r (using '0 7 * * *')", expr)
        trigger = CronExpressionTrigger.from_cron("0 7 * * *")
    tz = _local_tz()
    kwargs = {"timezone": tz} if tz is not None else {}
    scheduler = AsyncIOScheduler(**kwargs)
    scheduler.add_job(
        job,
        trigger=trigger,
        id="morning_run",
        replace_existing=True,
        misfire_grace_time=3600,  # skip if machine is sleeping at run time
    )
    scheduler.start()
    log.info("planned run started: cron '%s'", expr)
    return scheduler
