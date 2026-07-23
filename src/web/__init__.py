"""FastAPI web dashboard (Jinja2 templates, Pico CSS + HTMX)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.templating import Jinja2Templates

from core import db
from config import ACTIVITIES, ACTIVITY_OPTIONS
from core.dow import next_window

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fmt_dt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%a %d %b %Y, %H:%M")
    return str(value)


templates.env.filters["dt"] = _fmt_dt


def ctx(**kwargs) -> dict:
    return {"activities": ACTIVITIES, "activity_options": ACTIVITY_OPTIONS, **kwargs}


def job_row(job: db.Job) -> dict:
    """A job plus the derived scheduling info the templates display."""
    return {
        "job": job,
        # next time the booking window opens (None for a disabled job)
        "next_run": next_window(job.run_dow, job.run_hour, job.run_minute) if job.enabled else None,
        "target_preview": (date.today() + timedelta(days=job.date_offset)).strftime("%a %d %b"),
    }
