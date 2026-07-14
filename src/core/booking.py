"""The booking engine: finds and books a slot, recording the outcome.

Standalone (opens its own DB connections) so APScheduler and the CLI can
call it directly.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from core import db
from config import DELCOM_PASSWORD, DELCOM_USERNAME
from core.delcom import (
    LOCAL_TZ,
    DelcomClient,
    DelcomError,
    slot_is_free,
    slot_local_time,
)

log = logging.getLogger(__name__)

_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _dow_set(expr: str) -> set[int]:
    """Weekdays (Mon=0) matched by an APScheduler day_of_week expression
    like 'mon', 'mon-fri', 'sat,sun' or '*'. Falls back to every day if the
    expression cannot be parsed."""
    try:
        days: set[int] = set()
        for token in expr.lower().split(","):
            token = token.strip()
            if token == "*":
                return set(range(7))
            if "-" in token:
                a, b = (
                    int(t) % 7 if t.strip().isdigit() else _DOW[t.strip()]
                    for t in token.split("-", 1)
                )
                d = a
                while True:
                    days.add(d)
                    if d == b:
                        break
                    d = (d + 1) % 7
            elif token.isdigit():
                days.add(int(token) % 7)
            else:
                days.add(_DOW[token])
        return days
    except (KeyError, ValueError):
        log.warning("Cannot parse day-of-week expression %r", expr)
        return set(range(7))


def _next_target_date(job: db.Job) -> date:
    """The nearest date the job's slot occurs, for a manual "Run Now".

    A job books the weekday implied by its fire days + offset; this returns
    the earliest such date, counting today if the slot time is still ahead.
    (The scheduled cron run instead books offset days ahead, at the moment
    that window opens.)
    """
    fire_days = _dow_set(job.run_dow)
    today = date.today()
    now_hhmm = datetime.now(LOCAL_TZ).strftime("%H:%M")
    # offset+8 days always contain a full week of candidate fire days
    for i in range(job.date_offset + 8):
        d = today + timedelta(days=i)
        if (d - timedelta(days=job.date_offset)).weekday() not in fire_days:
            continue
        if d == today and job.slot_start_time <= now_hhmm:
            continue  # today's slot already started
        return d
    return today + timedelta(days=job.date_offset)


def _pick_slot(
    slots: list[dict],
    bookings: list[dict],
    slot_start_time: str,
    preferred_court_id: int | None,
    excluded_courts: set[int],
) -> dict | None:
    """Choose the best free slot matching the target time / court.

    Availability is cross-checked against the day's existing bookings
    because the slots endpoint's isAvailable flag is unreliable.
    """
    matches = [
        s
        for s in slots
        if slot_is_free(s, bookings)
        and slot_local_time(s) == slot_start_time
        and s.get("bookableProductId") not in excluded_courts
        and (
            preferred_court_id is None
            or s.get("bookableProductId") == preferred_court_id
        )
    ]
    if not matches:
        return None
    # prefer the exact requested court, else the lowest court id (stable)
    matches.sort(
        key=lambda s: (
            0 if s.get("bookableProductId") == preferred_court_id else 1,
            s.get("bookableProductId", 0),
        )
    )
    return matches[0]


def execute_booking(
    activity_product_id: int,
    target: date,
    slot_start_time: str,
    preferred_court_id: int | None = None,
    dry_run: bool = False,
    job_id: int | None = None,
) -> tuple[str, str]:
    """Make one booking pass. Returns ``(status, message)``.

    Queries the slots once and books immediately; the only fallback is
    trying the next free court when the server rejects one as blocked.

    Records a hsp_booking_attempts row per outcome; if the database is
    unreachable (e.g. CLI use without postgres) it logs a warning instead.
    """
    target_str = target.isoformat()

    def record(status: str, message: str, **kwargs) -> None:
        try:
            db.record_attempt(
                job_id, status, message,
                target_date=target_str, slot_start_time=slot_start_time, **kwargs,
            )
        except db.Unavailable as exc:
            log.warning("Could not record attempt in database: %s", exc)
        log.info("Attempt job=%s status=%s: %s", job_id, status, message)

    client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
    try:
        client.login()
        member = client.get_member()
    except Exception as exc:  # noqa: BLE001
        message = f"Login/member failed: {exc}"
        record("error", message)
        return "error", message

    try:
        slots = client.get_slots(activity_product_id, target)
    except Exception as exc:  # noqa: BLE001
        message = f"Slot query failed: {exc}"
        record("error", message)
        return "error", message
    try:
        bookings = client.get_court_bookings(
            {s["bookableProductId"] for s in slots}, target
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Bookings cross-check failed, trusting isAvailable: %s", exc)
        bookings = []

    # walk the candidate courts; a court the server rejects as blocked is
    # excluded and the next free one is tried right away
    last_error: str | None = None
    blocked_courts: set[int] = set()
    while True:
        slot = _pick_slot(
            slots, bookings, slot_start_time, preferred_court_id, blocked_courts
        )
        if slot is None:
            break

        court_id = slot["bookableProductId"]
        court_name = client.product_name(court_id) or f"Court {court_id}"
        if dry_run:
            message = f"DRY RUN — would book {court_name} at {slot_start_time}"
            record(
                "success", message,
                court_id=court_id, court_name=court_name,
                raw=json.dumps(slot, ensure_ascii=False),
            )
            return "dry_run", message
        try:
            booked = client.create_booking(member["id"], slot) or {}
        except DelcomError as exc:
            last_error = str(exc)
            record(
                "failed", last_error,
                court_id=court_id, court_name=court_name,
                raw=json.dumps(slot, ensure_ascii=False),
            )
            blocked_courts.add(court_id)
            continue

        # store the participation id — cancellation deletes /participations/{id};
        # the response's bookingId is a different entity and cannot be DELETEd
        booking_id = booked.get("id") if isinstance(booked, dict) else None
        message = f"Booked {court_name} on {target_str} at {slot_start_time}"
        record(
            "success", message,
            court_id=court_id, court_name=court_name, booking_id=booking_id,
            raw=json.dumps(booked, ensure_ascii=False),
        )
        return "success", message

    if last_error is not None:
        return "failed", last_error

    available = sorted(
        {slot_local_time(s) for s in slots if slot_is_free(s, bookings)}
    )
    message = (
        f"No free slot at {slot_start_time} on {target_str}. "
        f"Free times: {', '.join(available) or 'none'}"
    )
    record("no_slot", message)
    return "no_slot", message


def run_booking_job(job_id: int, next_occurrence: bool = False) -> str:
    """Execute one booking attempt for ``job_id``. Returns the status string.

    Safe to call from APScheduler or manually. With ``next_occurrence``
    (the "Run Now" button) the nearest date the job's slot occurs is booked
    instead of ``date_offset`` days ahead — clicking outside the cron fire
    day would otherwise target a day whose window isn't open yet.
    """
    job = db.get_job(job_id)
    if job is None:
        log.warning("Job %s no longer exists", job_id)
        return "missing"
    if not job.enabled:
        return "disabled"

    if next_occurrence:
        target = _next_target_date(job)
    else:
        target = date.today() + timedelta(days=job.date_offset)
    status, _message = execute_booking(
        activity_product_id=job.activity_product_id,
        target=target,
        slot_start_time=job.slot_start_time,
        preferred_court_id=job.preferred_court_id,
        job_id=job.id,
    )
    return status
