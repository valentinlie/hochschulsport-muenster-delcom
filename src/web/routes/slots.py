"""Live slot browser: GET /slots, POST /slots/book"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from config import ACTIVITY_OPTIONS, DELCOM_PASSWORD, DELCOM_USERNAME
from core.booking import execute_booking
from core.delcom import DelcomClient, slot_is_free, slot_local_end, slot_local_time
from web import ctx, templates
from web.auth import require_auth

router = APIRouter()


@router.get("/slots", response_class=HTMLResponse)
def slots(
    request: Request,
    option: str | None = None,
    day: str | None = None,
    _user: str = Depends(require_auth),
):
    rows = None
    bookings: list[dict] = []
    error = None
    selected = option if option in ACTIVITY_OPTIONS else next(iter(ACTIVITY_OPTIONS))
    opt = ACTIVITY_OPTIONS[selected]
    if option and day:
        try:
            target = date.fromisoformat(day)
            client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
            client.login()
            found = client.get_slots(opt["activity_product_id"], target)
            if opt["court_ids"] is not None:
                found = [
                    s for s in found if s["bookableProductId"] in opt["court_ids"]
                ]
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
            option=selected,
            activity=opt["activity_product_id"],
            day=day or default_day,
            slot_local_time=slot_local_time,
            slot_local_end=slot_local_end,
            is_free=lambda s: slot_is_free(s, bookings),
        ),
    )


@router.post("/slots/book", response_class=HTMLResponse)
def slots_book(
    request: Request,
    activity_product_id: int = Form(...),
    day: str = Form(...),
    slot_start_time: str = Form(...),
    end_time: str = Form(""),
    court_id: int = Form(...),
    linked_id: str = Form(""),
    row_id: int = Form(0),
    _user: str = Depends(require_auth),
):
    """Book one specific slot from the live browser; returns the re-rendered row.

    Delegates to the booking engine, which re-checks availability against
    the day's live bookings right before booking and records the attempt
    in the history.
    """

    def row(free: bool, booked: bool, message: str, message_type: str = "error"):
        return templates.TemplateResponse(
            request,
            "partials/slot_row.html",
            ctx(
                row_id=row_id,
                start=slot_start_time,
                end=end_time,
                court_id=court_id,
                linked_id=linked_id,
                free=free,
                booked=booked,
                activity=activity_product_id,
                day=day,
                message=message,
                message_type=message_type,
            ),
        )

    try:
        target = date.fromisoformat(day)
    except ValueError:
        return row(free=True, booked=False, message="Invalid day")
    status, message = execute_booking(
        activity_product_id=activity_product_id,
        target=target,
        slot_start_time=slot_start_time,
        preferred_court_id=court_id,
    )
    if status == "success":
        return row(free=False, booked=True, message=message, message_type="success")
    if status == "no_slot":
        # someone grabbed the court since the page was loaded
        return row(free=False, booked=False, message=message)
    # failed/error: the slot itself may still be free — keep the button for a retry
    return row(free=True, booked=False, message=message)
