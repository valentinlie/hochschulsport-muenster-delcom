"""FastAPI web dashboard (Jinja2 templates, Pico CSS + HTMX)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.templating import Jinja2Templates

from core import db
from config import ACTIVITIES
from core.scheduler import get_scheduler

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fmt_dt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%a %d %b %Y, %H:%M")
    return str(value)


templates.env.filters["dt"] = _fmt_dt


def ctx(**kwargs) -> dict:
    return {"activities": ACTIVITIES, **kwargs}


def job_row(job: db.Job) -> dict:
    """A job plus the live scheduler info the templates display."""
    sched_job = get_scheduler().get_job(f"job:{job.id}")
    return {
        "job": job,
        # pending jobs (scheduler not started yet) have no next_run_time
        "next_run": getattr(sched_job, "next_run_time", None),
        "target_preview": (date.today() + timedelta(days=job.date_offset)).strftime("%a %d %b"),
    }
