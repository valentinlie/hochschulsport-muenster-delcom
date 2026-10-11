"""Courses: GET /courses (browse + scheduled grabs), schedule / book now / delete."""

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import DELCOM_PASSWORD, DELCOM_USERNAME
from core import db
from core.courses import book_course, course_label, opens_at
from core.delcom import LOCAL_TZ, DelcomClient, _to_local
from core.systemd import remove_course_timer, sync_course_timer
from web import ctx, templates
from web.auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter()

# Shop course categories: the level-5 tags (the tags= of /shop/courses/list) of
# /courses/query/availableCourseTagsAndProductIds — together they cover every course.
COURSE_TAGS = {
    "72": "Erste Hilfe",
    "75": "Fit over 60",
    "30": "Fortbildungen",
    "362": "Fußball Frauen Wettkampfsport",
    "254": "Gesundheitssport",
    "253": "Padel Tennis Kurse",
    "255": "Schwimmen für Babys und Kleinkinder",
    "262": "Schwimmkurse für Erwachsene",
    "300": "Selbstverteidigung für Frauen",
    "321": "Ski",
    "333": "Sporttouren",
    "326": "Tauchen",
    "259": "Tennis Kurse",
    "324": "Windsurfen",
}
DEFAULT_TAGS = "259"


@router.get("/courses", response_class=HTMLResponse)
def courses(request: Request, tags: str = DEFAULT_TAGS, _user: str = Depends(require_auth)):
    rows, error = [], None
    try:
        client = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD)
        tag_ids = [int(t) for t in tags.split(",") if t.strip()]
        now = datetime.now(LOCAL_TZ)
        for c in client.get_courses(client.course_product_ids(tag_ids)):
            rows.append({
                "id": c["id"],
                "name": c["description"].strip(),
                "start": _to_local(c["startDate"]),
                "opens": opens_at(c),
                "is_open": opens_at(c) <= now,
                "free": c["availableParticipantCount"],
                "taken": c["currentParticipantCount"],
                "price": c.get("price"),
            })
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    jobs = db.get_all_course_jobs()
    return templates.TemplateResponse(
        request, "courses.html",
        ctx(active_page="courses", tags=tags, course_tags=COURSE_TAGS, rows=rows, error=error, jobs=jobs,
            scheduled={j.course_id for j in jobs}, now=datetime.now(LOCAL_TZ)),
    )


@router.post("/courses/{course_id}/schedule")
def course_schedule(course_id: int, tags: str = DEFAULT_TAGS, _user: str = Depends(require_auth)):
    course = DelcomClient(DELCOM_USERNAME, DELCOM_PASSWORD).get_course(course_id)
    job_id = db.create_course_job(course_label(course)[:200], course_id, opens_at(course))
    sync_course_timer(db.get_course_job(job_id))
    return RedirectResponse(url=f"/courses?tags={tags}", status_code=303)


@router.post("/courses/{course_id}/book", response_class=HTMLResponse)
def course_book_now(request: Request, course_id: int, _user: str = Depends(require_auth)):
    # off the request thread; the outcome lands in the history
    threading.Thread(target=book_course, args=(course_id,), daemon=True).start()
    return templates.TemplateResponse(
        request, "partials/alert.html",
        ctx(type="success", message="Enrolment queued — check history"),
    )


@router.post("/course-jobs/{job_id}/delete", response_class=HTMLResponse)
def course_job_delete(job_id: int, _user: str = Depends(require_auth)):
    remove_course_timer(job_id)
    db.delete_course_job(job_id)
    return HTMLResponse("")
