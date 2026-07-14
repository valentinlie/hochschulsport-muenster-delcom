"""Dashboard route: GET /"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from core import db
from config import DELCOM_USERNAME
from web import ctx, job_row, templates
from web.auth import require_auth

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _user: str = Depends(require_auth)):
    rows = [job_row(j) for j in db.get_all_jobs()]
    attempts = db.get_recent_attempts(limit=10)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        ctx(active_page="dashboard", rows=rows, attempts=attempts, account=DELCOM_USERNAME),
    )
