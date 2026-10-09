"""Self-check for the court grab + timer math, with a fake client (no network).
Run: uv run python tests/test_booking.py  (needs src/config.py)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from core import booking, db, systemd  # noqa: E402
from core.delcom import DelcomError, _rows  # noqa: E402

recorded = []
db.record_attempt = lambda job_id, status, message, **k: recorded.append(status)

DAY = date(2026, 7, 20)


def slot(court):  # 15:00 Berlin summer time = 13:00 UTC
    return {"bookableProductId": court, "linkedProductId": 1, "isAvailable": True,
            "startDate": "2026-07-20T13:00:00.000Z", "endDate": "2026-07-20T14:00:00.000Z"}


class Fake:
    def __init__(self, outcomes, taken_after_loss=False):
        self.outcomes = list(outcomes)  # per POST: "ok", "blocked" or "lost"
        self.taken_after_loss = taken_after_loss
        self.calls = []

    def get_slots(self, activity, day):
        self.calls.append("slots")
        return [slot(110), slot(111)]

    def get_court_bookings(self, courts, day):
        self.calls.append("bookings")
        return [slot(110) | {"productId": 110}] if self.taken_after_loss and "post" in self.calls else []

    def create_booking(self, member_id, s):
        self.calls.append("post")
        outcome = self.outcomes.pop(0)
        if outcome == "blocked":
            raise DelcomError("Booking failed (400): blocked by a booking")
        if outcome == "lost":
            raise requests.ConnectionError("reset")
        return {"id": 999}

    def product_name(self, court):
        self.calls.append("name")
        return f"Feld {court}"


def book(fake, **kw):
    recorded.clear()
    return booking.execute_booking(126, DAY, "15:00", client=fake, member={"id": 1}, **kw)


# happy path: name lookup + DB write only after the POST
f = Fake(["ok"])
status, msg = book(f)
assert status == "success" and "Feld 110" in msg, msg
assert f.calls == ["slots", "bookings", "post", "name"], f.calls

# prefetched bookings skip the fire-time cross-check
f = Fake(["ok"])
book(f, bookings=[])
assert f.calls[:2] == ["slots", "post"], f.calls

# blocked court -> next court, failure recorded after the grab
f = Fake(["blocked", "ok"])
status, msg = book(f, bookings=[])
assert status == "success" and "Feld 111" in msg and recorded == ["failed", "success"], (msg, recorded)

# lost response, slot still free -> retry the same court
f = Fake(["lost", "ok"])
status, msg = book(f, bookings=[])
assert status == "success" and "Feld 110" in msg, msg

# lost response, slot now taken (maybe by us) -> stop, never try court 111
f = Fake(["lost", "ok"], taken_after_loss=True)
status, msg = book(f, bookings=[])
assert status == "error" and "may have gone through" in msg and f.calls.count("post") == 1, msg

# keeps losing -> gives up after 3 tries instead of looping
f = Fake(["lost"] * 5)
status, msg = book(f, bookings=[])
assert status == "error" and f.calls.count("post") == 3, f.calls

# timer: 00:00 window fires 23:59 the day before, in Berlin time
assert systemd._fire_spec({0}, 0, 0) == ({6}, 23, 59)
assert systemd._fire_spec({5}, 15, 5) == ({5}, 15, 4)
assert _rows({"data": [1]}) == [1] and _rows([2]) == [2]

print("ok")
