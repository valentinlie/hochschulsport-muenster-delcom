"""Jobs routes: /jobs/*"""

import logging
import re
import threading
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import ACTIVITY_OPTIONS
from core import db
from core.booking import run_booking_job
from core.dow import dow_valid
from core.systemd import remove_job_timer, sync_job_timer
from web import ctx, job_row, templates
from web.auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter()

DOW_CHOICES = [
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
    ("*", "Every day"),
    ("mon-fri", "Weekdays"),
    ("sat,sun", "Weekend"),
]


def _validate(data: dict) -> str | None:
    """Return an error message for the job form, or None if it is valid."""
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", data["slot_start_time"]):
        return "Slot start time must be HH:MM, e.g. 15:00."
    if data["activity_product_id"] <= 0:
        return "Activity product id must be a positive number."
    if not 0 <= data["date_offset"] <= 60:
        return "Offset must be between 0 and 60 days."
    if not 0 <= data["run_hour"] <= 23:
        return "Fire hour must be between 0 and 23."
    if not 0 <= data["run_minute"] <= 59:
        return "Fire minute must be between 0 and 59."
    if not dow_valid(data["run_dow"]):
        return f"Invalid day-of-week expression: {data['run_dow']!r}."
    return None


def _option_for(job: db.Job) -> str:
    """The select value for a job: its saved option, or (for jobs saved
    before options existed) the first option matching its activity id."""
    if job.activity_option in ACTIVITY_OPTIONS:
        return job.activity_option
    return next(
        (
            key
            for key, opt in ACTIVITY_OPTIONS.items()
            if opt["activity_product_id"] == job.activity_product_id
        ),
        next(iter(ACTIVITY_OPTIONS)),
    )


@router.get("/jobs/new", response_class=HTMLResponse)
def job_new(request: Request, _user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        request,
        "job_form.html",
        ctx(job=None, dow_choices=DOW_CHOICES,
            selected_option=next(iter(ACTIVITY_OPTIONS))),
    )


@router.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
def job_edit(request: Request, job_id: int, _user: str = Depends(require_auth)):
    job = db.get_job(job_id)
    if job is None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "job_form.html",
        ctx(job=job, dow_choices=DOW_CHOICES, selected_option=_option_for(job)),
    )


@router.post("/jobs", response_class=HTMLResponse)
@router.post("/jobs/{job_id}", response_class=HTMLResponse)
def job_save(
    request: Request,
    job_id: int | None = None,
    name: str = Form(""),
    enabled: bool = Form(False),
    activity_option: str = Form(...),
    preferred_court_id: str = Form(""),
    slot_start_time: str = Form("08:00"),
    date_offset: int = Form(7),
    run_dow: str = Form("*"),
    run_hour: int = Form(0),
    run_minute: int = Form(0),
    _user: str = Depends(require_auth),
):
    option = ACTIVITY_OPTIONS.get(activity_option)
    data = {
        "name": name.strip() or "Unnamed job",
        "enabled": enabled,
        "activity_product_id": option["activity_product_id"] if option else 0,
        "activity_option": activity_option,
        "preferred_court_id": None,
        "slot_start_time": slot_start_time,
        "date_offset": date_offset,
        "run_dow": run_dow,
        "run_hour": run_hour,
        "run_minute": run_minute,
    }
    error = None
    if option is None:
        error = "Unknown activity option."
    if preferred_court_id.strip():
        try:
            data["preferred_court_id"] = int(preferred_court_id)
        except ValueError:
            error = error or "Preferred court id must be a number."
    error = error or _validate(data)
    if error is not None:
        return templates.TemplateResponse(
            request,
            "job_form.html",
            ctx(job=SimpleNamespace(id=job_id, **data), dow_choices=DOW_CHOICES,
                error=error,
                selected_option=(
                    activity_option
                    if activity_option in ACTIVITY_OPTIONS
                    else next(iter(ACTIVITY_OPTIONS))
                )),
            status_code=422,
        )
    if job_id is None:
        job_id = db.create_job(data)
    elif db.get_job(job_id) is None:
        return RedirectResponse(url="/", status_code=303)
    else:
        db.update_job(job_id, data)
    sync_job_timer(db.get_job(job_id))
    return RedirectResponse(url="/", status_code=303)


@router.post("/jobs/{job_id}/toggle", response_class=HTMLResponse)
def job_toggle(request: Request, job_id: int, _user: str = Depends(require_auth)):
    db.toggle_job(job_id)
    job = db.get_job(job_id)
    if job is None:
        return HTMLResponse("")
    sync_job_timer(job)
    return templates.TemplateResponse(request, "partials/job_row.html", ctx(row=job_row(job)))


@router.post("/jobs/{job_id}/delete", response_class=HTMLResponse)
def job_delete(request: Request, job_id: int, _user: str = Depends(require_auth)):
    remove_job_timer(job_id)
    db.delete_job(job_id)
    # Return empty string so HTMX removes the row
    return HTMLResponse("")


@router.post("/jobs/{job_id}/run", response_class=HTMLResponse)
def job_run_now(request: Request, job_id: int, _user: str = Depends(require_auth)):
    if db.get_job(job_id) is None:
        return templates.TemplateResponse(
            request, "partials/alert.html", ctx(type="error", message="Job not found")
        )
    # Manual runs book the job's next occurring slot day immediately (no
    # window spin-wait); run off the request thread so the UI returns at once.
    threading.Thread(
        target=run_booking_job, args=(job_id,), kwargs={"next_occurrence": True}, daemon=True
    ).start()
    return templates.TemplateResponse(
        request,
        "partials/alert.html",
        ctx(type="success", message="Booking attempt queued — check history"),
    )
