"""Manage the systemd --user units: one booking timer per job + the web UI.

Same scale-to-zero shape as the other bots, with one twist for a time-critical
grab: the timer fires ``LEAD_MINUTES`` *before* the window opens so the worker
can log in and then spin-wait to the exact second (see ``core/worker.py``).

- ``hsp-book@<id>.timer`` → ``hsp-book@<id>.service`` (oneshot ``run-job <id>``).
- ``hsp-web.socket`` socket-activates ``hsp-web.service`` on the first HTTP hit;
  the app shuts itself down when idle. ``SuccessExitStatus=143`` so that
  self-shutdown (SIGTERM) reads as clean, not ``failed``.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from config import PORT
from core import db
from core.dow import LOCAL_TZ, SYSTEMD_DAY, dow_set

log = logging.getLogger(__name__)

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
PROJECT_DIR = Path(__file__).resolve().parents[2]  # src/core/systemd.py -> repo root
UV = shutil.which("uv") or "uv"

BOOK_TEMPLATE = "hsp-book@.service"
COURSE_TEMPLATE = "hsp-course@.service"
WEB_SOCKET = "hsp-web.socket"
WEB_SERVICE = "hsp-web.service"

# How early the timer fires; the worker consumes the rest by spin-waiting to the
# exact window second. Must stay >= the worker's cold-start + login time and
# <= its MAX_WAIT. A whole minute keeps the OnCalendar arithmetic simple.
LEAD_MINUTES = 1


def _systemctl(*args: str) -> int:
    try:
        return subprocess.run(["systemctl", "--user", *args]).returncode
    except FileNotFoundError:
        log.warning("systemctl not found; skipping: %s", " ".join(args))
        return 1


def _write(name: str, content: str) -> None:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / name).write_text(content)


def _timer_name(job_id: int) -> str:
    return f"hsp-book@{job_id}.timer"


def _fire_spec(day_set: set[int], run_hour: int, run_minute: int) -> tuple[set[int], int, int]:
    """(fire_days, hour, minute) for firing LEAD_MINUTES before the window.

    Handles the wrap when the lead crosses midnight (e.g. a 00:00 window fires
    at 23:59 the *previous* day, so every fire day shifts back one).
    """
    total = run_hour * 60 + run_minute - LEAD_MINUTES
    if total < 0:
        total += 24 * 60
        day_set = {(d - 1) % 7 for d in day_set}
    hour, minute = divmod(total, 60)
    return day_set, hour, minute


def _on_calendar(job: db.Job) -> str:
    days, hour, minute = _fire_spec(dow_set(job.run_dow), *job.run_time)
    day_spec = ",".join(SYSTEMD_DAY[d] for d in sorted(days))
    # explicit zone: the window is Berlin time whatever the host's TZ is
    return f"{day_spec} *-*-* {hour:02d}:{minute:02d}:00 Europe/Berlin"


def sync_job_timer(job: db.Job | None) -> None:
    """Create/update the timer for a job to match the DB, or remove it."""
    if job is None:
        return
    if not job.enabled:
        remove_job_timer(job.id)
        return

    on_calendar = _on_calendar(job)
    # A missed grab must never fire late (the window is long gone), so no
    # Persistent catch-up.
    _write(_timer_name(job.id), f"""[Unit]
Description=HSP booking job {job.id} ({job.name})

[Timer]
OnCalendar={on_calendar}
AccuracySec=1s
Persistent=false
Unit=hsp-book@{job.id}.service

[Install]
WantedBy=timers.target
""")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", _timer_name(job.id))
    log.info("Synced timer for job %d: fires at OnCalendar=%s (%d min before window)",
             job.id, on_calendar, LEAD_MINUTES)


def remove_job_timer(job_id: int) -> None:
    name = _timer_name(job_id)
    _systemctl("disable", "--now", name)
    path = UNIT_DIR / name
    if path.exists():
        path.unlink()
    _systemctl("daemon-reload")


def _course_timer_name(job_id: int) -> str:
    return f"hsp-course@{job_id}.timer"


def sync_course_timer(job: db.CourseJob) -> None:
    """A one-shot timer LEAD_MINUTES before enrolment opens (a full date, so
    it fires once and never again)."""
    fire = job.opens_at.astimezone(LOCAL_TZ) - timedelta(minutes=LEAD_MINUTES)
    on_calendar = fire.strftime("%Y-%m-%d %H:%M:%S") + " Europe/Berlin"
    _write(_course_timer_name(job.id), f"""[Unit]
Description=HSP course enrolment {job.id} ({job.name})

[Timer]
OnCalendar={on_calendar}
AccuracySec=1s
Persistent=false
Unit=hsp-course@{job.id}.service

[Install]
WantedBy=timers.target
""")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", _course_timer_name(job.id))
    log.info("Synced course timer %d: OnCalendar=%s", job.id, on_calendar)


def remove_course_timer(job_id: int) -> None:
    name = _course_timer_name(job_id)
    _systemctl("disable", "--now", name)
    path = UNIT_DIR / name
    if path.exists():
        path.unlink()
    _systemctl("daemon-reload")


def sync_all_jobs() -> None:
    for job in db.get_all_jobs():
        sync_job_timer(job)
    now = datetime.now(LOCAL_TZ)
    for course_job in db.get_all_course_jobs():
        if course_job.opens_at > now:  # past ones already fired
            sync_course_timer(course_job)


def install() -> None:
    """Write the shared units (booking template + web UI) and reload systemd."""
    _write(BOOK_TEMPLATE, f"""[Unit]
Description=HSP booking worker for job %i

[Service]
Type=oneshot
WorkingDirectory={PROJECT_DIR}
ExecStart={UV} run python src/cli.py run-job %i
""")
    _write(COURSE_TEMPLATE, f"""[Unit]
Description=HSP course enrolment worker for job %i

[Service]
Type=oneshot
WorkingDirectory={PROJECT_DIR}
ExecStart={UV} run python src/cli.py run-course %i
""")
    _write(WEB_SOCKET, f"""[Unit]
Description=HSP booking web UI socket (on-demand)

[Socket]
ListenStream={PORT}

[Install]
WantedBy=sockets.target
""")
    _write(WEB_SERVICE, f"""[Unit]
Description=HSP booking web UI
Requires={WEB_SOCKET}
After={WEB_SOCKET}

[Service]
WorkingDirectory={PROJECT_DIR}
ExecStart={UV} run python src/cli.py web --fd 3
# The idle watchdog stops the UI with SIGTERM (exit 143) — treat that as clean.
SuccessExitStatus=143
""")
    _systemctl("daemon-reload")
