"""FastAPI application with APScheduler lifespan."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.db import close_pool, init_db
from core.scheduler import shutdown_scheduler, start_scheduler
from web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    shutdown_scheduler()
    close_pool()


app = FastAPI(title="hochschulsport-muenster-delcom", lifespan=lifespan)
app.include_router(router)
