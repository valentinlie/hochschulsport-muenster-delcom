# hochschulsport-muenster-delcom bot

Books courts on [hochschulsportmuenster.de](https://hochschulsportmuenster.de) the
instant the booking window opens (e.g. grab a tennis court exactly one week ahead).

- **SSO** via FH Münster Shibboleth — no headless browser, just `requests` + BeautifulSoup.
- **Scheduling** with systemd --user timers, one per job (scale-to-zero — nothing
  runs between grabs). Each timer fires a minute early so the worker can pre-warm the
  login and then spin-wait to the exact window second (Europe/Berlin).
- **Web dashboard** with FastAPI + Jinja2 templates (Pico CSS + HTMX), protected by
  a passkey (WebAuthn) — manage jobs, run tests, browse history, query live slots.
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
attempt on these weekdays at this time.”* Because a court opens exactly `offset` days
before the slot, you point the fire time at the moment the window opens:

> Want a **Saturday 15:00** court, booked **7 days** ahead?
> Set `offset = 7`, fire on **Saturday 00:00**. It computes `target = today + 7 days`,
> queries the 15:00 slots once, and immediately books the first free court (or your
> preferred court id).

Each enabled job becomes a `hsp-book@<id>.timer`. To keep the grab instant, the timer
is set to fire **one minute before** the window (the `OnCalendar` is computed from the
job's weekdays/time, shifting across midnight when needed). The worker
(`core/worker.py`) uses that lead to log in, then **spin-waits to the exact window
second** and books with the already-authenticated session — so neither the process
cold-start nor the (slow) login sits on the critical path. Changing a job in the
dashboard re-syncs its timer automatically.

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

# ── Dashboard login (WebAuthn / passkey) ──────────────────────────────────────
# False drops authentication entirely and leaves the dashboard open — handy for
# a purely local run. Everything below is then unused. See "Passkeys".
AUTH_ENABLED = True

# RP_ID is the bare hostname the dashboard is served from — no scheme, no port.
# ORIGIN is the full origin your browser shows. The site must be HTTPS.
RP_ID = "hsp.example.de"
RP_NAME = "Hochschulsport Münster Bot"
ORIGIN = "https://hsp.example.de"

# python -c "import secrets; print(secrets.token_urlsafe(32))"
SESSION_SECRET = "change_me"
SESSION_MAX_AGE = 30 * 24 * 3600

# Single-use enrolment token: /passkeys?token=<value>. Empty closes enrolment
# to everyone but an already signed-in session.
REGISTRATION_TOKEN = ""

# ── Server ────────────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8000
```

## Deployment (systemd, scale-to-zero)

Scheduling and serving are handled by **systemd --user units**, so nothing of ours
stays resident when idle:

- each enabled job → a `hsp-book@<id>.timer` firing a short-lived pre-warming worker;
- the dashboard is **socket-activated** — `hsp-web.socket` starts the app on the first
  request, and the app shuts itself down after 10 min idle (`HSP_WEB_IDLE_TIMEOUT`).

```bash
uv run python src/cli.py install   # write the units + a timer per saved job
uv run python src/cli.py enable     # enable + start the web socket
uv run python src/cli.py status     # list job timers + web-UI status
uv run python src/cli.py logs -f    # tail worker + web logs
```

> **Enable lingering** so timers fire even when you are not logged in:
> `sudo loginctl enable-linger "$USER"` (on managed hosts like Uberspace it is
> already on). Run `sync` after pulling code changes to rewrite the units.

For a quick manual/dev run without systemd (stays up, no idle-shutdown):

```bash
uv run src/main.py                          # honors HOST/PORT from src/config.py
uv run python src/cli.py web                # same, via the CLI
```

Open the dashboard behind your HTTPS reverse proxy (the host you set as
`ORIGIN`) and unlock it with your **passkey**. To enrol the first key, set
`REGISTRATION_TOKEN` in `config.py` and visit
`https://hsp.valentinl.de/passkeys?token=<token>` — see [Passkeys](#passkeys).

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

The `hsp` CLI (`uv run python src/cli.py <command>`) has booking, web, and service
commands. For quick one-off bookings without the web interface, use `book`:

```bash
# book a tennis court 7 days from today at 15:00, any free court
uv run python src/cli.py book --offset 7 --time 15:00

# specific court on a specific date, dry run (stops before booking)
uv run python src/cli.py book --activity 126 --court 110 --date 2026-07-20 --time 15:00 --dry-run

# just list the slots for a day (availability + court ids)
uv run python src/cli.py book --offset 7 --list
```

Other commands: `jobs` (list saved jobs), `run-job <id>` (fire a saved job — the
timer uses this), and the service commands above. Known activity ids are listed with
`uv run python src/cli.py book --help`. CLI attempts are recorded in the history like
scheduled ones (skipped with a warning if PostgreSQL isn't running).

## Passkeys

The dashboard authenticates with WebAuthn ([py_webauthn](https://github.com/duo-labs/py_webauthn)) — no password anywhere. Keys live in the `hsp_credentials` table.

**Turning auth off.** For a purely local run, set `AUTH_ENABLED = False` in `config.py`. The dashboard is then served with no login at all: `/login` and `/passkeys` redirect to the dashboard, the ceremony endpoints return 404, and the Passkeys/Log out controls disappear. Your registered keys stay in the database untouched, so flipping it back to `True` restores exactly where you left off. The app logs a warning on every start while it is off — only do this when the dashboard is not reachable from the network.

**Requirements.** WebAuthn only runs in a secure context, so the dashboard must be served over **HTTPS** (the sole exception browsers make is `localhost`). `RP_ID` must equal the hostname exactly — `hsp.example.de`, not `https://hsp.example.de` and not `hsp.example.de:8000`. A passkey is cryptographically bound to `RP_ID`, so if you later move the dashboard to a different hostname you must register a new one.

**Enrolling a key.** There is no anonymous enrolment — a fresh deployment is never up for grabs. Registering requires either an already signed-in session or the `REGISTRATION_TOKEN` from `config.py`, passed as `https://hsp.valentinl.de/passkeys?token=<token>`. The token is validated once, remembered in the session, then dropped from the URL by a redirect; registering spends the unlock and signs you in. A wrong token is logged and changes nothing. The token is **single-use**: registering with it records its SHA-256 in `hsp_consumed_tokens` and the link stops working, so it cannot be replayed out of your browser history or the reverse proxy's access log. Adding further keys while signed in does not spend it. To enrol again, put a new value in `config.py`.

**Managing keys.** Once signed in, the *Passkeys* nav entry lists the registered keys and lets you add or remove them — no token needed while signed in. Adding a second one (e.g. your phone as well as your laptop) is worth doing.

**Locked out?** Set a fresh `REGISTRATION_TOKEN` in `config.py`, restart, and enrol again via `/passkeys?token=...`. Only if you also want to clear the old keys:

```bash
psql -d vali -c "DELETE FROM hsp_credentials"
```

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
    │   ├── booking.py          # Booking engine: one pass, records the attempt
    │   ├── worker.py           # Scheduled worker: pre-warm login, spin-wait, grab
    │   ├── dow.py              # Weekday-expression + next-window helpers (dependency-light)
    │   ├── systemd.py          # Generate per-job timers (fire early) + web units
    │   └── db.py               # PostgreSQL access (jobs + attempt history)
    │
    └── web/
        ├── app.py              # FastAPI app (socket-activated, idle-shutdown)
        ├── auth.py             # Passkey (WebAuthn) login + session guard
        ├── routes/
        │   ├── dashboard.py    # GET /
        │   ├── jobs.py         # Job CRUD + manual/dry run
        │   ├── history.py      # GET /history
        │   └── slots.py        # GET /slots (live slot browser)
        └── templates/          # Jinja2 templates (Pico CSS + HTMX)
```

## Notes

- The bearer token is long-lived (~7 days) and is **cached to
  `~/.cache/hsp-delcom/token.json`** (override with `HSP_TOKEN_CACHE`), so
  back-to-back bookings and separate timer-fired processes reuse one login
  instead of re-running the SSO flow — repeated rapid logins are what make the
  IdP answer with no token. The client re-logs in automatically when the token
  is missing/expired or a request returns 401, and `login()` retries once with a
  short backoff, logging the final page if it still fails.
- Scheduling is owned by systemd timers, not an in-process scheduler, so the web UI
  can come and go freely — there is no long-running process to keep single.
- Put the socket-activated web UI behind your reverse proxy (on Uberspace, a
  `uberspace web backend` pointed at the configured `PORT`).
