"""The booking engine: finds and books a slot, recording the outcome.

Standalone (opens its own DB connections) so the timer worker, the web UI and
the CLI can call it directly.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Iterable

import requests

from core import db
from config import ACTIVITY_OPTIONS, DELCOM_PASSWORD, DELCOM_USERNAME
from core.delcom import (
    LOCAL_TZ,
    DelcomClient,
    DelcomError,
    slot_is_free,
    slot_local_time,
)
from core.dow import dow_set as _dow_set

log = logging.getLogger(__name__)


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


def _still_free(client: DelcomClient, slot: dict, target: date) -> bool:
    """After a lost response: True only if the slot is provably still unbooked."""
    try:
        return slot_is_free(
            slot, client.get_court_bookings({slot["bookableProductId"]}, target)
        )
    except Exception:  # noqa: BLE001 — can't tell, so assume the POST may have landed
        return False


def execute_booking(
    activity_product_id: int,
    target: date,
    slot_start_time: str,
    preferred_court_id: int | None = None,
    allowed_court_ids: Iterable[int] | None = None,
    dry_run: bool = False,
    job_id: int | None = None,
    client: DelcomClient | None = None,
    member: dict | None = None,
    bookings: list[dict] | None = None,
) -> tuple[str, str]:
    """Make one booking pass. Returns ``(status, message)``.

    ``allowed_court_ids`` restricts booking to those courts (an activity like
    tennis 126 spans several locations; the court set selects one).

    ``client``/``member``/``bookings`` let the scheduled worker pass a
    pre-warmed session and the day's existing bookings fetched during its lead
    time, so only the slot query and the POST sit on the critical path. When
    omitted, they are fetched here.

    Queries the slots once and books immediately; the only fallback is
    trying the next free court when the server rejects one as blocked.

    Records a hsp_booking_attempts row per outcome — after the grab, so neither
    the DB nor the court-name lookup slows it down. If the database is
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

    if client is None:
        client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
    try:
        if member is None:
            client.ensure_login()  # reuses the cached ~7-day token when valid
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
    if allowed_court_ids is not None:
        allowed = set(allowed_court_ids)
        slots = [s for s in slots if s.get("bookableProductId") in allowed]
    if bookings is None:
        try:
            bookings = client.get_court_bookings(
                {s["bookableProductId"] for s in slots}, target
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Bookings cross-check failed, trusting isAvailable: %s", exc)
            bookings = []

    # The grab: walk the candidate courts; a court the server rejects as blocked
    # is excluded and the next free one is tried right away. Nothing slow
    # (DB writes, name lookups) happens inside this loop.
    failures: list[tuple[int, str, dict]] = []  # (court, error, slot), recorded below
    blocked_courts: set[int] = set()
    chosen: dict | None = None
    booked: dict | None = None
    unknown: str | None = None  # set when a POST got no answer and may have landed
    lost = 0
    while True:
        slot = _pick_slot(
            slots, bookings, slot_start_time, preferred_court_id, blocked_courts
        )
        if slot is None:
            break
        court_id = slot["bookableProductId"]
        if dry_run:
            chosen = slot
            break
        try:
            booked = client.create_booking(member["id"], slot) or {}
            chosen = slot
            break
        except DelcomError as exc:
            failures.append((court_id, str(exc), slot))
            blocked_courts.add(court_id)
        except requests.RequestException as exc:
            # No answer: the booking may or may not exist. Retry this court only
            # while it is provably still free — never move on to another court,
            # or we could end up holding two.
            lost += 1
            if lost >= 3 or not _still_free(client, slot, target):
                chosen, unknown = slot, str(exc)
                break
            failures.append((court_id, f"No answer, retrying: {exc}", slot))

    def court_name(court_id: int) -> str:
        return client.product_name(court_id) or f"Court {court_id}"

    for court_id, error, slot in failures:
        record(
            "failed", error, court_id=court_id, court_name=court_name(court_id),
            raw=json.dumps(slot, ensure_ascii=False),
        )

    if chosen is not None:
        court_id = chosen["bookableProductId"]
        name = court_name(court_id)
        if dry_run:
            message = f"DRY RUN — would book {name} at {slot_start_time}"
            record(
                "success", message, court_id=court_id, court_name=name,
                raw=json.dumps(chosen, ensure_ascii=False),
            )
            return "dry_run", message
        if unknown is not None:
            message = (
                f"No answer booking {name} on {target_str} at {slot_start_time} "
                f"({unknown}); it may have gone through — check the site"
            )
            record(
                "error", message, court_id=court_id, court_name=name,
                raw=json.dumps(chosen, ensure_ascii=False),
            )
            return "error", message
        # store the participation id — cancellation deletes /participations/{id};
        # the response's bookingId is a different entity and cannot be DELETEd
        booking_id = booked.get("id") if isinstance(booked, dict) else None
        message = f"Booked {name} on {target_str} at {slot_start_time}"
        record(
            "success", message, court_id=court_id, court_name=name,
            booking_id=booking_id, raw=json.dumps(booked, ensure_ascii=False),
        )
        return "success", message

    if failures:
        return "failed", failures[-1][1]

    available = sorted(
        {slot_local_time(s) for s in slots if slot_is_free(s, bookings)}
    )
    message = (
        f"No free slot at {slot_start_time} on {target_str}. "
        f"Free times: {', '.join(available) or 'none'}"
    )
    record("no_slot", message)
    return "no_slot", message


def job_courts(job: db.Job) -> tuple[int, ...] | None:
    """The courts a job may book (its activity option's court set), or None = any."""
    option = ACTIVITY_OPTIONS.get(job.activity_option or "")
    return option["court_ids"] if option else None


def book_job(job: db.Job, target: date, **kwargs) -> tuple[str, str]:
    """:func:`execute_booking` for a saved job on ``target``."""
    return execute_booking(
        activity_product_id=job.activity_product_id,
        target=target,
        slot_start_time=job.slot_start_time,
        preferred_court_id=job.preferred_court_id,
        allowed_court_ids=job_courts(job),
        job_id=job.id,
        **kwargs,
    )


def run_booking_job(job_id: int, next_occurrence: bool = False) -> str:
    """Execute one booking attempt for ``job_id`` right now. Returns the status.

    With ``next_occurrence`` (the "Run Now" button) the nearest date the job's
    slot occurs is booked instead of ``date_offset`` days ahead — clicking
    outside the fire day would otherwise target a day whose window isn't open
    yet. (Scheduled runs go through ``core.worker.run_job``.)
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
    status, _message = book_job(job, target)
    return status
