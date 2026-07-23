"""FastAPI dashboard for the Delcom booking bot.

Scheduling lives in systemd timers now (see ``core/systemd.py``); this app only
serves the UI. It is socket-activated and shuts itself down once idle so it does
not sit resident between visits.
"""

import asyncio
import logging
import os
import signal
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from core.db import close_pool, init_db
from web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Shut the socket-activated UI down after this many idle seconds. 0 disables the
# watchdog (set by `cli web` / `main.py` for manual runs).
IDLE_TIMEOUT = int(os.environ.get("HSP_WEB_IDLE_TIMEOUT", "600"))


async def _idle_watchdog(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(30)
        idle = time.monotonic() - app.state.last_request
        if idle >= IDLE_TIMEOUT:
            log.info("Web UI idle for %.0fs, shutting down (socket stays active)", idle)
            os.kill(os.getpid(), signal.SIGTERM)
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.last_request = time.monotonic()
    watchdog = asyncio.create_task(_idle_watchdog(app)) if IDLE_TIMEOUT > 0 else None
    yield
    if watchdog:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
    close_pool()


app = FastAPI(title="hochschulsport-muenster-delcom", lifespan=lifespan)


@app.middleware("http")
async def _track_activity(request: Request, call_next):
    request.app.state.last_request = time.monotonic()
    return await call_next(request)


app.include_router(router)
