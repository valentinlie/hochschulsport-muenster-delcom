"""FastAPI web dashboard (Jinja2 templates, Pico CSS + HTMX)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.templating import Jinja2Templates

from core import db
from config import ACTIVITIES, ACTIVITY_OPTIONS, AUTH_ENABLED
from core.dow import next_window
from core.window import WINDOW_OPEN_MINUTE
from web.auth import is_authenticated

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fmt_dt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%a %d %b %Y, %H:%M")
    return str(value)


templates.env.filters["dt"] = _fmt_dt
# base.html only draws the nav for a signed-in visitor; ctx() has no request, so
# the templates ask directly.
templates.env.globals["is_authenticated"] = is_authenticated
templates.env.globals["auth_enabled"] = AUTH_ENABLED
# The job form derives its fire time from the slot time in the browser too.
templates.env.globals["window_open_minute"] = WINDOW_OPEN_MINUTE


def ctx(**kwargs) -> dict:
    return {"activities": ACTIVITIES, "activity_options": ACTIVITY_OPTIONS, **kwargs}


def job_row(job: db.Job) -> dict:
    """A job plus the derived scheduling info the templates display."""
    hour, minute = job.run_time
    return {
        "job": job,
        "fire_time": f"{hour:02d}:{minute:02d}",
        # next time the booking window opens (None for a disabled job)
        "next_run": next_window(job.run_dow, hour, minute) if job.enabled else None,
        "target_preview": (date.today() + timedelta(days=job.date_offset)).strftime("%a %d %b"),
    }
