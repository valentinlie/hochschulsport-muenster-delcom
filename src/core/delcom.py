"""Client for the Delcom backbone API behind hochschulsportmuenster.de.

Implements the FH Münster Shibboleth SSO login and the slot / booking
endpoints, using only ``requests`` + BeautifulSoup (no headless browser).

See the reverse-engineering notes in the project README for the flow.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

API_BASE = "https://backbone-web-api.production.munster.delcom.nl"
FRONTEND = "https://hochschulsportmuenster.de"
IDP_ENTITY_ID = "https://idp.fh-muenster.de/idp/shibboleth"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
LOCAL_TZ = ZoneInfo("Europe/Berlin")


class DelcomError(RuntimeError):
    """Raised when the API returns an unexpected response."""


class LoginError(DelcomError):
    """Raised when SSO login fails (bad credentials, changed flow, ...)."""


def _iso_utc(dt: datetime) -> str:
    """Format an aware datetime the way Delcom emits them: UTC with .000Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _local_day_utc_range(day: date) -> tuple[datetime, datetime]:
    """The [midnight, midnight) window of a Europe/Berlin day, as aware UTC."""
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(
        timezone.utc
    )


def _to_local(iso_utc: str) -> datetime:
    """Parse a Delcom timestamp ('...T06:00:00.000Z', real UTC) to Europe/Berlin."""
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(LOCAL_TZ)


def slot_local_time(slot: dict) -> str:
    """Return the slot's Europe/Berlin start time as 'HH:MM'.

    Delcom timestamps are real UTC (since ~July 2026; they used to be local
    wall-clock with a decorative 'Z').
    """
    return _to_local(slot["startDate"]).strftime("%H:%M")


def slot_local_end(slot: dict) -> str:
    """Return the slot's Europe/Berlin end time as 'HH:MM'."""
    return _to_local(slot["endDate"]).strftime("%H:%M")


def slot_local_date(slot: dict) -> str:
    return _to_local(slot["startDate"]).strftime("%Y-%m-%d")


def slot_is_free(slot: dict, bookings: Iterable[dict]) -> bool:
    """True if no existing booking on the slot's court overlaps the slot.

    The bookable-slots endpoint's own ``isAvailable`` flag cannot be trusted:
    it reports true even for times that already carry a booking (observed
    July 2026 — the server then rejects the POST with "blocked by a booking").
    Callers should cross-check against ``get_court_bookings``.

    Timestamps compare as strings; both endpoints emit the same
    'YYYY-MM-DDTHH:MM:SS.000Z' format.
    """
    if not slot.get("isAvailable"):
        return False
    start, end = slot["startDate"], slot["endDate"]
    return not any(
        b.get("productId") == slot.get("bookableProductId")
        and b["startDate"] < end
        and start < b["endDate"]
        for b in bookings
    )


class DelcomClient:
    def __init__(self, username: str, password: str, timeout: int = 20):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.token: dict[str, Any] | None = None
        self.member: dict[str, Any] | None = None

    # -- auth ---------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if not self.token:
            raise DelcomError("Not logged in")
        return {
            "Authorization": f"Bearer {self.token['access_token']}",
            "x-platform": "CF",
            "x-custom-lang": "de",
        }

    def login(self) -> dict[str, Any]:
        """Perform the FH Münster Shibboleth SSO flow and store the token."""
        if not self.username or not self.password:
            raise LoginError("Missing username/password")

        r = self.session.get(
            f"{API_BASE}/saml/discovery-callback",
            params={
                "callbackUrl": f"{FRONTEND}/pages/token-callback",
                "entityId": IDP_ENTITY_ID,
            },
            allow_redirects=True,
            timeout=self.timeout,
        )
        r.raise_for_status()

        for _ in range(10):
            soup = BeautifulSoup(r.text, "html.parser")
            form = soup.find("form")
            if form is None:
                break
            action = urljoin(r.url, form.get("action", ""))
            fields: dict[str, str] = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    fields[name] = inp.get("value", "")

            if "j_username" in fields or any("username" in k.lower() for k in fields):
                fields["j_username"] = self.username
                fields["j_password"] = self.password
                fields["_eventId_proceed"] = ""
                fields.pop("_eventId_authn/SPNEGO", None)
            elif "SAMLResponse" in fields:
                pass  # auto-submit the assertion back to the SP
            else:
                # consent / continue pages
                fields.setdefault("_eventId_proceed", "")

            r = self.session.post(
                action, data=fields, allow_redirects=True, timeout=self.timeout
            )
            q = parse_qs(urlparse(r.url).query)
            if "tokenReponse" in q or "tokenResponse" in q:
                break
            if "j_password" in r.text and "error" in r.text.lower():
                raise LoginError("IdP rejected the credentials")

        q = parse_qs(urlparse(r.url).query)
        blob = (q.get("tokenReponse") or q.get("tokenResponse") or [None])[0]
        if not blob:
            raise LoginError(
                "SSO did not return a token — credentials wrong or flow changed"
            )
        self.token = json.loads(base64.b64decode(blob))
        log.info("Logged in as %s", self.username)
        return self.token

    def ensure_login(self) -> None:
        if self.token is None:
            self.login()

    # -- requests -----------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        self.ensure_login()
        p = dict(params or {})
        p.setdefault("cf", 0)  # bypass the app-version gate
        r = self.session.get(
            f"{API_BASE}{path}",
            headers=self._auth_headers(),
            params=p,
            timeout=self.timeout,
        )
        if r.status_code == 401:
            # token expired -> re-login once
            self.token = None
            self.ensure_login()
            r = self.session.get(
                f"{API_BASE}{path}",
                headers=self._auth_headers(),
                params=p,
                timeout=self.timeout,
            )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict, params: dict | None = None) -> requests.Response:
        self.ensure_login()
        p = dict(params or {})
        p.setdefault("cf", 0)
        r = self.session.post(
            f"{API_BASE}{path}",
            headers=self._auth_headers(),
            params=p,
            json=body,
            timeout=self.timeout,
        )
        return r

    # -- domain endpoints ---------------------------------------------------

    def get_member(self) -> dict[str, Any]:
        self.member = self._get("/auth")
        return self.member

    def get_slots(
        self, activity_product_id: int, day: date
    ) -> list[dict[str, Any]]:
        """Return bookable slots for the given activity product on ``day``
        (a Europe/Berlin calendar day).

        Note: the returned ``isAvailable`` flags are unreliable — cross-check
        with ``get_court_bookings`` / ``slot_is_free``.
        """
        start, end = _local_day_utc_range(day)
        s = json.dumps(
            {
                "activityProductIds": {"$in": [activity_product_id]},
                "startDate": {"$gte": _iso_utc(start)},
                "endDate": {"$lte": _iso_utc(end)},
            }
        )
        data = self._get("/products/bookable-slots", {"s": s})
        return data.get("data", data) if isinstance(data, dict) else data

    def get_court_bookings(
        self, court_ids: Iterable[int], day: date
    ) -> list[dict[str, Any]]:
        """Existing bookings on the given courts during the local day.

        Includes member bookings and admin blocks ("Nachholtermine" etc.) —
        exactly what the server's overlap check rejects a new booking against.
        """
        ids = sorted(set(court_ids))
        if not ids:
            return []
        start, end = _local_day_utc_range(day)
        s = json.dumps(
            {
                "productId": {"$in": ids},
                "startDate": {"$gte": _iso_utc(start)},
                "endDate": {"$lte": _iso_utc(end)},
            }
        )
        data = self._get("/bookings", {"s": s, "limit": 500})
        return data.get("data", data) if isinstance(data, dict) else data

    def create_booking(self, member_id: int, slot: dict) -> dict[str, Any]:
        """Book a slot. Echoes the slot's own datetimes/ids back verbatim.

        Returns the parsed API response on success; raises DelcomError on
        failure with the server message.
        """
        body = {
            "organizationId": None,
            "memberId": member_id,
            "params": {
                "startDate": slot["startDate"],
                "endDate": slot["endDate"],
                "bookableProductId": slot["bookableProductId"],
                "bookableLinkedProductId": slot["linkedProductId"],
                "clickedOnBook": True,
            },
        }
        r = self._post("/participations", body)
        if not r.ok:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise DelcomError(f"Booking failed ({r.status_code}): {msg}")
        return r.json()

    def _delete_participation(self, participation_id: int) -> requests.Response:
        self.ensure_login()
        return self.session.delete(
            f"{API_BASE}/participations/{participation_id}",
            headers=self._auth_headers(),
            params={"cf": 0},
            timeout=self.timeout,
        )

    def cancel_booking(self, participation_id: int) -> None:
        """Cancel a booking by deleting its participation ("Stornieren").

        Expects the participation id from the booking response's ``id``
        field. If the given id is actually a booking id (older history rows
        stored those), the matching participation is looked up and deleted
        instead.
        """
        r = self._delete_participation(participation_id)
        if r.ok:
            return
        if r.status_code in (403, 404):
            member = self.member or self.get_member()
            s = json.dumps(
                {"bookingId": participation_id, "memberId": member["id"]}
            )
            rows = self._get("/participations", {"s": s, "limit": 1})
            rows = rows.get("data", rows) if isinstance(rows, dict) else rows
            if rows:
                r = self._delete_participation(rows[0]["id"])
                if r.ok:
                    return
        try:
            msg = r.json().get("message", r.text)
        except Exception:
            msg = r.text
        raise DelcomError(f"Cancellation failed ({r.status_code}): {msg}")

    def product_name(self, product_id: int) -> str | None:
        """Best-effort German display name for a product id."""
        try:
            data = self._get(
                "/products",
                {
                    "s": json.dumps({"id": product_id}),
                    "join": ["translations"],
                    "limit": 1,
                },
            )
            rows = data.get("data", data) if isinstance(data, dict) else data
            if not rows:
                return None
            tr = rows[0].get("translations") or []
            de = next(
                (t.get("description") for t in tr if t.get("language") == "de"), None
            )
            return de or next((t.get("description") for t in tr if t.get("description")), None)
        except Exception:
            return None
