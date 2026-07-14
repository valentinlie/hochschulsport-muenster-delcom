"""Live slot browser: GET /slots"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from config import DELCOM_PASSWORD, DELCOM_USERNAME
from core.delcom import DelcomClient, slot_is_free, slot_local_end, slot_local_time
from web import ctx, templates
from web.auth import require_auth

router = APIRouter()


@router.get("/slots", response_class=HTMLResponse)
def slots(
    request: Request,
    activity_product_id: int | None = None,
    day: str | None = None,
    _user: str = Depends(require_auth),
):
    rows = None
    bookings: list[dict] = []
    error = None
    if activity_product_id and day:
        try:
            target = date.fromisoformat(day)
            client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
            client.login()
            found = client.get_slots(activity_product_id, target)
            bookings = client.get_court_bookings(
                {s["bookableProductId"] for s in found}, target
            )
            rows = sorted(
                found, key=lambda s: (slot_local_time(s), s.get("bookableProductId", 0))
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    default_day = (date.today() + timedelta(days=7)).isoformat()
    return templates.TemplateResponse(
        request,
        "slots.html",
        ctx(
            active_page="slots",
            rows=rows,
            error=error,
            activity=activity_product_id,
            day=day or default_day,
            slot_local_time=slot_local_time,
            slot_local_end=slot_local_end,
            is_free=lambda s: slot_is_free(s, bookings),
        ),
    )
