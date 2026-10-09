"""One-off course enrolment: grab a course spot the second enrolment opens.

Unlike courts, a course is not a recurring slot — it has a single, fixed
``startDateEnrollment`` (e.g. all tennis courses opened 2026-10-08 10:00 and
sold out within minutes). Enrolling = ``POST /sales`` with the course as the
only sale item; that is what the shop's "In den Warenkorb" → "Checkout" does.
For a paid course the sale comes back *pending*; right after the grab the bot
pays it with the member's saved SEPA direct-debit mandate (``POST /sales/<id>/pay``,
the checkout's "Bezahlen"). If that is not possible the sale stays pending and
you pay on the site (``/sales/<id>/checkout``).

The worker (``run_course_job``) is fired by ``hsp-course@<id>.timer`` a minute
early, pre-builds the request, spin-waits to the opening second, then fires and
keeps retrying for ``RETRY_SECONDS`` — the server clock may lag ours slightly,
and at peak the API answers with errors — stopping early once the course is full.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from config import DELCOM_PASSWORD, DELCOM_USERNAME
from core import db
from core.delcom import FRONTEND, LOCAL_TZ, DelcomClient, _to_local

log = logging.getLogger(__name__)

RETRY_SECONDS = 60.0  # keep trying this long after the opening second
RETRY_DELAY = 0.1  # pause between tries (on top of the course re-check)
MAX_WAIT = 180.0  # never sleep longer than this waiting for the opening


def opens_at(course: dict) -> datetime:
    """When enrolment opens, as an aware Europe/Berlin datetime."""
    return _to_local(course["startDateEnrollment"])


def course_label(course: dict) -> str:
    start = _to_local(course["startDate"]).strftime("%a %d.%m. %H:%M")
    return f"{course['description'].strip()} ({start}, #{course['id']})"


def book_course(
    course_id: int,
    *,
    job_id: int | None = None,
    retry_seconds: float = 0.0,
    dry_run: bool = False,
    client: DelcomClient | None = None,
    member: dict | None = None,
    course: dict | None = None,
    body: dict | None = None,
    pay: bool = True,
) -> tuple[str, str]:
    """Enrol in a course. Returns ``(status, message)`` and records the attempt.

    With ``pay`` a paid course is paid right after the grab with the saved SEPA
    mandate; otherwise (or if that fails) the sale stays pending for the site.

    ``client``/``member``/``course``/``body`` let the worker pass a pre-warmed
    session and a pre-built request so only the POST sits on the critical path.
    """
    label = f"course #{course_id}"
    course = course or {}

    def record(status: str, message: str, raw: str | None = None) -> None:
        try:
            db.record_attempt(
                None, status, message,
                target_date=course.get("startDate", "")[:10] or None,
                court_id=course_id, court_name=label[:200], raw=raw,
            )
        except db.Unavailable as exc:
            log.warning("Could not record attempt in database: %s", exc)
        log.info("Course attempt job=%s status=%s: %s", job_id, status, message)

    try:
        if client is None:
            client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
        if member is None:
            client.ensure_login()
            member = client.get_member()
        if body is None:
            course = client.get_course(course_id)
            body = client.course_sale_body(course, member, client.sales_person_id())
    except Exception as exc:  # noqa: BLE001
        message = f"Preparing enrolment failed: {exc}"
        record("error", message)
        return "error", message
    if course.get("description"):
        label = course_label(course)

    if dry_run:
        try:
            method = client.direct_debit_method(member["id"]) if pay else None
        except Exception as exc:  # noqa: BLE001
            method = f"lookup failed: {exc}"
        message = (
            f"DRY RUN — would POST /sales for {label}: {json.dumps(body)}; "
            f"then pay with: {_describe(method) if pay else 'nothing (pay off)'}"
        )
        return "dry_run", message

    deadline = time.monotonic() + retry_seconds
    tries = 0
    last_error = ""
    while True:
        tries += 1
        try:
            sale = client.create_sale(body)
        except Exception as exc:  # noqa: BLE001 — rejected, 5xx or network: retry
            last_error = str(exc)
        else:
            message, raw = _pay(client, member, course, sale, label, pay)
            if tries > 1:
                message += f" [try {tries}]"
            record("success", message, raw=raw)
            return "success", message

        if time.monotonic() >= deadline:
            break
        try:  # stop early once nothing is left to grab
            fresh = client.get_course(course_id)
            if fresh.get("availableParticipantCount", 1) <= 0:
                message = f"{label} is full ({tries} tries). Last error: {last_error}"
                record("no_slot", message)
                return "no_slot", message
        except Exception:  # noqa: BLE001
            pass
        time.sleep(RETRY_DELAY)

    message = f"{last_error} ({tries} tries)" if tries > 1 else last_error
    record("failed", message)
    return "failed", message


def _describe(method) -> str:
    if isinstance(method, dict):
        return f"saved direct debit #{method['id']} ({method.get('description')})"
    return method or "nothing — no saved direct-debit mandate"


def _pay(client, member: dict, course: dict, sale, label: str, pay: bool) -> tuple[str, str]:
    """Settle a freshly created sale. Returns ``(message, raw)``; never raises —
    the spot is grabbed either way, a failed payment just leaves it pending."""
    sale = sale if isinstance(sale, dict) else {}
    sale_id = sale.get("id")
    checkout = f"{FRONTEND}/sales/{sale_id}/checkout"
    raw = json.dumps(sale, ensure_ascii=False)
    if sale.get("statusCode") == 0:  # DONE: a free course
        return f"Enrolled in {label} (sale #{sale_id}, nothing to pay)", raw
    pending = f"Spot reserved in {label} — PAY NOW to keep it: {checkout}"
    if not pay:
        return pending, raw

    # Money guard: never pay more than the course's full list price.
    limit, total = course.get("price"), sale.get("total")
    try:
        if limit is None or total is None or float(total) > float(limit):
            return f"{pending} (not auto-paid: total {total} € vs list price {limit} €)", raw
    except (TypeError, ValueError):
        return f"{pending} (not auto-paid: unreadable total {total!r})", raw

    try:
        method = client.direct_debit_method(member["id"])
        if method is None:
            return f"{pending} (no saved direct-debit mandate)", raw
        # One try only: a retried payment could charge twice.
        paid = client.pay_sale(sale_id, method)
    except Exception as exc:  # noqa: BLE001
        return f"{pending} (auto-pay failed: {exc})", raw
    raw = json.dumps({"sale": sale, "pay": paid}, ensure_ascii=False)
    result = (paid or {}).get("userInitiatedPaymentResult") or {}
    done = ((paid or {}).get("sale") or {}).get("statusCode") == 0
    if result.get("bPaidOnStoredPayMethod") or (not result and done):
        return f"Enrolled in {label} and paid {total} € by direct debit (sale #{sale_id})", raw
    why = result.get("message") or (
        "needs the payment page" if result.get("hostedPaymentPageUrl") else "not confirmed"
    )
    return f"{pending} (auto-pay {why})", raw


def run_course_job(job_id: int) -> str:
    """Timer entry point: pre-warm, spin-wait to the opening second, grab."""
    job = db.get_course_job(job_id)
    if job is None:
        log.warning("Course job %s no longer exists", job_id)
        return "missing"

    client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
    try:
        client.ensure_login()
        member = client.get_member()
        course = client.get_course(job.course_id)
        body = client.course_sale_body(course, member, client.sales_person_id())
    except Exception as exc:  # noqa: BLE001 — book_course retries the prep cold
        log.warning("Course job %s pre-warm failed (%s); preparing at fire time", job_id, exc)
        client, member, body, course = None, None, None, None

    # Trust the live opening time over the stored one (it can be moved).
    window = opens_at(course) if course else job.opens_at.astimezone(LOCAL_TZ)
    wait = (window - datetime.now(LOCAL_TZ)).total_seconds()
    if wait > MAX_WAIT:
        log.warning("Course job %s: enrolment opens %s, too far away — not firing", job_id, window)
        return "early"
    log.info("Course job %s pre-warmed; %.2fs before enrolment opens", job_id, wait)
    if wait > 3 and client is not None:
        # wake the (possibly idled) keep-alive connection just before the grab
        time.sleep(wait - 2)
        try:
            client.get_course(job.course_id)
        except Exception:  # noqa: BLE001
            pass
    wait = (window - datetime.now(LOCAL_TZ)).total_seconds()
    if wait > 0:
        time.sleep(wait)

    status, message = book_course(
        job.course_id, job_id=job.id, retry_seconds=RETRY_SECONDS,
        client=client, member=member, course=course, body=body,
    )
    log.info("Course job %s finished: %s — %s", job_id, status, message)
    return status
