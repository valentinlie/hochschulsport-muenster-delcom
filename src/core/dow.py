"""Weekday-expression helpers shared by the timer generator, the booking
worker, and the web dashboard — kept dependency-light on purpose so importing
it (e.g. to write a unit file) does not drag in the HTTP client.

The day-of-week expression is the same little grammar the old APScheduler
``day_of_week`` accepted: ``mon``, ``mon-fri``, ``sat,sun``, ``*`` or numeric
(Mon=0 … Sun=6).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Berlin")

_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
SYSTEMD_DAY = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def dow_set(expr: str) -> set[int]:
    """Weekdays (Mon=0) matched by a day-of-week expression. Falls back to
    every day if the expression cannot be parsed."""
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


def dow_valid(expr: str) -> bool:
    """Strict check for the job form — rejects unknown tokens instead of
    silently falling back the way :func:`dow_set` does."""
    if not expr.strip():
        return False
    try:
        for token in expr.lower().split(","):
            token = token.strip()
            if token == "*":
                continue
            for part in (token.split("-", 1) if "-" in token else [token]):
                part = part.strip()
                if part.isdigit():
                    if not 0 <= int(part) <= 7:
                        return False
                elif part not in _DOW:
                    return False
        return True
    except ValueError:
        return False


def next_window(run_dow: str, run_hour: int, run_minute: int) -> datetime | None:
    """The next datetime the booking window opens (run_dow at HH:MM, Berlin)."""
    days = dow_set(run_dow)
    now = datetime.now(LOCAL_TZ)
    for i in range(8):
        d = (now + timedelta(days=i)).date()
        if d.weekday() in days:
            cand = datetime(d.year, d.month, d.day, run_hour, run_minute, tzinfo=LOCAL_TZ)
            if cand > now:
                return cand
    return None
