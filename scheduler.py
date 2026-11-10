"""Расписание утреннего прогона (APScheduler, cron из .env RUN_CRON)."""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
    """Запускает планировщик с ежедневным заданием (минута, час из RUN_CRON)."""
    minute, hour = settings.run_cron
    tz = _local_tz()
    kwargs = {"timezone": tz} if tz is not None else {}
    scheduler = AsyncIOScheduler(**kwargs)
    scheduler.add_job(
        job,
        trigger="cron",
        minute=minute,
        hour=hour,
        id="morning_run",
        replace_existing=True,
        misfire_grace_time=3600,  # пропуск, если машина спала в момент запуска
    )
    scheduler.start()
    log.info("планировщик запущен: ежедневный прогон в %02d:%02d", hour, minute)
    return scheduler
