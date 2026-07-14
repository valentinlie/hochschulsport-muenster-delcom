# hochschulsport-muenster-delcom bot

Books courts on [hochschulsportmuenster.de](https://hochschulsportmuenster.de) the
instant the booking window opens (e.g. grab a tennis court exactly one week ahead).

- **SSO** via FH Münster Shibboleth — no headless browser, just `requests` + BeautifulSoup.
- **Scheduling** with APScheduler (cron triggers, Europe/Berlin).
- **Web dashboard** with FastAPI + Jinja2 templates (Pico CSS + HTMX), protected by
  HTTP Basic Auth — manage jobs, run tests, browse history, query live slots.
- **PostgreSQL** stores the scheduled jobs and every booking attempt (raw `psycopg`).
  The database must already exist — point `DB_NAME` in `config.py` at it. All tables the
  bot owns are prefixed `hsp_` (`hsp_booking_jobs`, `hsp_booking_attempts`) and created
  on first startup, so the database can be shared with other projects.

## How it works

The bot talks directly to the Delcom "backbone" JSON API behind the site — no
browser or heavy frontend involved:

1. **Login** — `GET /saml/discovery-callback` → follow the FH Münster Shibboleth
   flow (`j_username` / `j_password`) → the IdP posts a `SAMLResponse` back to
   `/saml/callback`, which redirects with a base64 `tokenReponse` (a JWT bearer token).
2. **Slots** — `GET /products/bookable-slots?cf=0&s=<json>` returns the slot grid
   for an activity product on a day. **Its `isAvailable` flag is unreliable** (it
   says `true` even for times that already carry a booking), so the bot
   cross-checks every slot against `GET /bookings` for the same courts/day and
   only books truly free ones. If the server still rejects a court with
   *"blocked by a booking"*, the bot excludes that court and immediately tries
   the next free one.
3. **Book** — `POST /participations?cf=0` with the chosen slot. This is the
   single call behind the two “Buchen” clicks (open confirm modal → confirm).

Every request needs `?cf=0` (an app-version gate) plus `x-platform: CF`.
Datetimes are **real UTC** (`2026-07-20T06:00:00.000Z` = 08:00 German summer
time). They used to be local wall-clock with a decorative `Z`; Delcom changed
the convention around July 2026. All times you enter (jobs, CLI `--time`) are
Europe/Berlin local times and get converted.

### Scheduling model

A **job** says *“book the slot `offset` days ahead at `slot_start_time`, and fire the
attempt on this cron schedule.”* Because a court opens exactly `offset` days before
the slot, you point the cron trigger at the moment the window opens:

> Want a **Saturday 15:00** court, booked **7 days** ahead?
> Set `offset = 7`, fire on **Saturday 00:01**. When it fires it computes
> `target = today + 7 days`, queries the 15:00 slots once, and immediately books the
> first free court (or your preferred court id).

## Setup

Requires Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and a running PostgreSQL
server (the `delcom_bot` database itself is created automatically).

### 1. Install Python dependencies

```bash
uv sync
```

### 2. Create `src/config.py`

Copy the example below and fill in your credentials. This file is git-ignored.

```python
"""Configuration for the Delcom booking bot. This file is git-ignored."""

# ── SSO credentials (FH Münster) ─────────────────────────────────────────────
DELCOM_USERNAME = "your_sso_username"
DELCOM_PASSWORD = "your_sso_password"

# ── Activity products (slot-query ids — discover more via "Live slots") ──────
ACTIVITIES = {
    126: "Tennisplatz (Platzbuchung) – Horstmarer Landweg",
    129: "Padel Tennis (Platzmiete)",
}

# ── PostgreSQL (database is created automatically on first startup) ──────────
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "delcom_bot"
DB_USER = "your_db_user"
DB_PASS = "your_db_password"

# ── Dashboard credentials (HTTP Basic Auth) ───────────────────────────────────
DASHBOARD_USER = "admin"
DASHBOARD_PASS = "change_me"

# ── Server ────────────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8000
```

## Usage

Start the server (web dashboard + scheduler):

```bash
# honors HOST/PORT from src/config.py
uv run src/main.py

# or with the uvicorn CLI — it ignores HOST/PORT (defaults to 127.0.0.1:8000),
# so pass the bind address explicitly:
uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8004
```

Open the dashboard at the address you bound and log in with your
`DASHBOARD_USER` / `DASHBOARD_PASS` credentials.

From the dashboard you can:

- **New job** — pick the activity product (126 = Tennisplatz Platzbuchung),
  optionally a preferred court, the slot time, the `offset`, and the cron fire time.
- **Run Now** — books the job's **next occurring slot day** immediately (today
  counts if the slot time is still ahead). Unlike the scheduled run — which
  fires when the window opens and books `offset` days ahead — Run Now grabs
  the nearest matching day that is already bookable. (Dry runs are CLI-only:
  `uv run src/cli.py ... --dry-run`.)
- **Live slots** — query availability now and discover court ids.
- **History** — every attempt with status (`success` / `no_slot` / `failed` / `error`);
  successful bookings have a **Cancel booking** button that storniert them again.

### CLI

For quick one-off bookings without the web interface:

```bash
# book a tennis court 7 days from today at 15:00, any free court
uv run src/cli.py --offset 7 --time 15:00

# specific court on a specific date, dry run (stops before booking)
uv run src/cli.py --activity 126 --court 110 --date 2026-07-20 --time 15:00 --dry-run

# just list the slots for a day (availability + court ids)
uv run src/cli.py --offset 7 --list
```

Known activity ids are listed with `uv run src/cli.py --help`. CLI attempts are
recorded in the history like scheduled ones (skipped with a warning if
PostgreSQL isn't running).

## Product ids (tennis)

| id  | name |
|-----|------|
| 126 | Tennisplatz (Platzbuchung) — courts 110/111/112 (Horstmarer Landweg Feld 1–3) and 191/192 (Sentruper Höhe Platz 8/9) |
| 129 | Padel Tennis (Platzmiete) |

Note: activity 126 spans **two locations**. Without a preferred court id the
bot books the lowest free court id, i.e. Horstmarer Landweg (110–112) first
and Sentruper Höhe (191/192) only when all Feld 1–3 are taken. Set a
preferred court id to pin the booking to a single specific court.

Use **Live slots** to find court ids for other facilities.

## Project structure

```
hochschulsport-muenster-delcom/
├── pyproject.toml
└── src/
    ├── config.py               # Credentials and settings (git-ignored)
    ├── main.py                 # Web entry point (uvicorn)
    ├── cli.py                  # CLI entry point
    │
    ├── core/
    │   ├── delcom.py           # Delcom backbone API client (SSO login, slots, booking)
    │   ├── booking.py          # Booking engine: executes a job, records the attempt
    │   ├── db.py               # PostgreSQL access (jobs + attempt history)
    │   └── scheduler.py        # APScheduler job management
    │
    └── web/
        ├── app.py              # FastAPI app with scheduler lifespan
        ├── auth.py             # HTTP Basic Auth
        ├── routes/
        │   ├── dashboard.py    # GET /
        │   ├── jobs.py         # Job CRUD + manual/dry run
        │   ├── history.py      # GET /history
        │   └── slots.py        # GET /slots (live slot browser)
        └── templates/          # Jinja2 templates (Pico CSS + HTMX)
```

## Notes

- The bearer token is long-lived (~7 days); the client re-logs in automatically on 401.
- Run a single instance — the scheduler runs in-process and `main.py` starts
  uvicorn without `--reload` so triggers aren't duplicated.
- For production, put it behind a process manager (systemd/supervisor) and a reverse proxy.
