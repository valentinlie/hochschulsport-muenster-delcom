"""When a booking window opens.

Delcom releases courts **per slot**, not per day: the whole day's grid does not
exist at midnight. Observed on the day the Aug 9 grid was watched — at 09:00 it
was still completely empty, and each hour's three courts appeared one hour-block
at a time, a few minutes past their own hour.

So the slot at ``HH:MM`` on day *D* becomes bookable at
``HH:WINDOW_OPEN_MINUTE`` on day ``D - date_offset``: the fire *time* follows
from the slot time, and only the fire *weekday* is a free choice. A job can
still opt out via ``run_time_manual`` and pin its own hour/minute.

Dependency-light on purpose (like ``core.dow``) — the timer generator, the
worker and the dashboard all import it.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Minutes past the hour at which the hour's courts show up.
WINDOW_OPEN_MINUTE = 5


def window_open_time(slot_start_time: str) -> tuple[int, int]:
    """``(hour, minute)`` the window for an ``HH:MM`` slot opens.

    Keyed off the slot's *hour* — the release goes by hour-block, so a slot at
    15:00 and (were there one) at 15:30 both open at 15:05.
    """
    try:
        hour = int(slot_start_time.split(":", 1)[0])
    except (AttributeError, IndexError, ValueError):
        log.warning("Cannot read an hour from slot start time %r", slot_start_time)
        return 0, WINDOW_OPEN_MINUTE
    return hour % 24, WINDOW_OPEN_MINUTE
