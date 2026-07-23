"""The scheduled booking worker: pre-warm, spin-wait, then grab.

Fired by ``hsp-book@<id>.timer`` a minute before the window opens. It logs in
*during* that lead time so the cold-start and the (slow) login are off the
critical path, then busy-waits to the exact window second and does the booking
pass with the already-authenticated client. This is faster off the mark than
the old always-resident scheduler, which only started logging in at fire time.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from config import ACTIVITY_OPTIONS, DELCOM_PASSWORD, DELCOM_USERNAME
from core import db
from core.booking import execute_booking
from core.delcom import DelcomClient
from core.dow import LOCAL_TZ

log = logging.getLogger(__name__)

# Never sleep longer than this waiting for the window — a guard against a
# misconfigured/oversized lead so the worker can't hang.
MAX_WAIT = 180.0


def run_job(job_id: int) -> str:
    job = db.get_job(job_id)
    if job is None:
        log.warning("Job %s no longer exists", job_id)
        return "missing"
    if not job.enabled:
        log.info("Job %s is disabled, skipping", job_id)
        return "disabled"

    # The window opens today at run_hour:run_minute; if that moment has already
    # passed (e.g. a 00:00 window whose timer fired at 23:59), roll to tomorrow.
    now = datetime.now(LOCAL_TZ)
    window = now.replace(hour=job.run_hour, minute=job.run_minute, second=0, microsecond=0)
    if window <= now:
        window += timedelta(days=1)
    target = window.date() + timedelta(days=job.date_offset)
    option = ACTIVITY_OPTIONS.get(job.activity_option or "")

    # Pre-warm: authenticate now, while there is still lead time.
    client: DelcomClient | None = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
    member = None
    try:
        client.ensure_login()  # cached token if valid, else one SSO login
        member = client.get_member()
        lead = (window - datetime.now(LOCAL_TZ)).total_seconds()
        log.info("Job %s pre-warmed; %.2fs before window opens", job_id, lead)
    except Exception as exc:  # noqa: BLE001 — fall back to a cold login in execute_booking
        log.warning("Job %s pre-warm login failed (%s); will retry at fire time", job_id, exc)
        client, member = None, None

    # Spin-wait to the exact window second, then fire immediately.
    wait = (window - datetime.now(LOCAL_TZ)).total_seconds()
    if 0 < wait <= MAX_WAIT:
        time.sleep(wait)

    status, message = execute_booking(
        activity_product_id=job.activity_product_id,
        target=target,
        slot_start_time=job.slot_start_time,
        preferred_court_id=job.preferred_court_id,
        allowed_court_ids=option["court_ids"] if option else None,
        job_id=job.id,
        client=client,
        member=member,
    )
    log.info("Job %s finished: %s — %s", job_id, status, message)
    return status
