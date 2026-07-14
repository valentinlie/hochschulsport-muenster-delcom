"""APScheduler wiring — keeps cron triggers in sync with hsp_booking_jobs rows."""
from __future__ import annotations

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import db
from core.booking import run_booking_job

log = logging.getLogger(__name__)

TIMEZONE = "Europe/Berlin"

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TIMEZONE)
    return _scheduler


def _job_key(job_id: int) -> str:
    return f"job:{job_id}"


def sync_job(job: db.Job) -> None:
    """Add/replace/remove the APScheduler trigger for one booking job."""
    sched = get_scheduler()
    key = _job_key(job.id)
    existing = sched.get_job(key)
    if not job.enabled:
        if existing:
            sched.remove_job(key)
        return
    trigger = CronTrigger(
        day_of_week=job.run_dow,
        hour=job.run_hour,
        minute=job.run_minute,
        timezone=TIMEZONE,
    )
    sched.add_job(
        run_booking_job,
        trigger=trigger,
        args=[job.id],
        id=key,
        name=job.name,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    log.info(
        "Scheduled job %s (%s) -> %s %02d:%02d",
        job.id, job.name, job.run_dow, job.run_hour, job.run_minute,
    )


def remove_job(job_id: int) -> None:
    sched = get_scheduler()
    if sched.get_job(_job_key(job_id)):
        sched.remove_job(_job_key(job_id))


def run_now(job_id: int) -> None:
    """Fire a one-off booking attempt immediately (from the UI).

    Manual runs book the job's next occurring slot day, not offset days
    ahead like the scheduled grab at window opening.
    """
    sched = get_scheduler()
    sched.add_job(
        run_booking_job,
        args=[job_id],
        kwargs={"next_occurrence": True},
        id=f"manual:{job_id}:{int(time.time() * 1000)}",
        misfire_grace_time=60,
    )


def start_scheduler() -> BackgroundScheduler:
    """Start the scheduler and load all enabled jobs from the database."""
    sched = get_scheduler()
    if not sched.running:
        sched.start()
    for job in db.get_all_jobs():
        sync_job(job)
    return sched


def shutdown_scheduler() -> None:
    sched = get_scheduler()
    if sched.running:
        sched.shutdown()
