"""History routes: GET /history, POST /attempts/{id}/cancel"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from core import db
from config import DELCOM_PASSWORD, DELCOM_USERNAME
from core.delcom import DelcomClient
from web import ctx, templates
from web.auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, _user: str = Depends(require_auth)):
    attempts = db.get_recent_attempts(limit=200)
    return templates.TemplateResponse(
        request, "history.html", ctx(active_page="history", attempts=attempts)
    )


@router.post("/attempts/{attempt_id}/cancel", response_class=HTMLResponse)
def attempt_cancel(request: Request, attempt_id: int, _user: str = Depends(require_auth)):
    attempt = db.get_attempt(attempt_id)
    if attempt is None:
        return HTMLResponse("")
    error = None
    if not attempt.booking_id or attempt.cancelled_at:
        error = "Nothing to cancel"
    else:
        try:
            client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
            client.login()
            client.cancel_booking(attempt.booking_id)
            db.mark_attempt_cancelled(attempt_id)
            attempt = db.get_attempt(attempt_id)
            log.info(
                "Cancelled booking %s (history entry %s)", attempt.booking_id, attempt_id
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    return templates.TemplateResponse(
        request, "partials/attempt_row.html", ctx(a=attempt, error=error)
    )
