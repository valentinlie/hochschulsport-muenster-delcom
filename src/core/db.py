"""PostgreSQL database for booking jobs + attempt history."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import psycopg
from psycopg.errors import InvalidCatalogName
from psycopg.rows import class_row, dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS

# What callers catch when postgres is unreachable (direct connect vs. pool
# raise different exceptions).
Unavailable = (psycopg.OperationalError, PoolTimeout)

# This bot is a guest in a database shared with other projects: it owns only the
# hsp_booking_jobs, hsp_booking_attempts, hsp_credentials and hsp_consumed_tokens
# tables and must never read, write or alter anything else. Any table added here
# carries the same hsp_ prefix.


@dataclass
class Job:
    id: int
    name: str
    enabled: bool
    activity_product_id: int
    # key into config.ACTIVITY_OPTIONS (restricts booking to that location's
    # courts); NULL on jobs saved before options existed = any court
    activity_option: str | None
    preferred_court_id: int | None
    slot_start_time: str
    date_offset: int
    run_dow: str
    run_hour: int
    run_minute: int
    created_at: datetime


@dataclass
class Attempt:
    id: int
    job_id: int | None
    created_at: datetime
    status: str
    message: str
    target_date: str | None
    slot_start_time: str | None
    court_id: int | None
    court_name: str | None
    booking_id: int | None
    raw: str | None
    cancelled_at: datetime | None


@dataclass
class Credential:
    """A registered WebAuthn passkey. ``credential_id`` is base64url text."""
    credential_id: str
    public_key: bytes
    sign_count: int
    transports: str | None
    label: str | None
    created_at: datetime
    last_used_at: datetime | None


def _conninfo(dbname: str) -> str:
    return psycopg.conninfo.make_conninfo(
        host=DB_HOST, port=DB_PORT, dbname=dbname, user=DB_USER, password=DB_PASS
    )


_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _conninfo(DB_NAME),
            min_size=1,
            max_size=4,
            timeout=5,  # raise PoolTimeout instead of hanging when postgres is down
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """Pooled connection; commits on success, rolls back on exception."""
    with get_pool().connection() as conn:
        yield conn


def init_db() -> None:
    """Create this bot's tables in the pre-existing, shared database.

    Never creates or drops a database, and only ever issues CREATE TABLE IF NOT
    EXISTS for the hsp_ tables it owns — a table without that prefix belongs
    to another project.

    Uses a direct connection (not the pool) so the pool is only ever opened
    against a database whose tables are known to exist.
    """
    try:
        conn = psycopg.connect(_conninfo(DB_NAME))
    except psycopg.OperationalError as exc:
        # connect() errors carry no sqlstate, so also match the server message
        missing = (
            getattr(exc, "sqlstate", None) == InvalidCatalogName.sqlstate
            or f'database "{DB_NAME}" does not exist' in str(exc)
        )
        if missing:
            raise RuntimeError(
                f'Database "{DB_NAME}" does not exist. This bot does not create '
                f"databases — it adds its hsp_ tables to an existing one. Point "
                f"DB_NAME in config.py at the database you want to use."
            ) from exc
        raise
    with conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hsp_booking_jobs (
                id                  SERIAL PRIMARY KEY,
                name                VARCHAR(200) NOT NULL,
                enabled             BOOLEAN NOT NULL DEFAULT TRUE,
                activity_product_id INT NOT NULL,
                activity_option     VARCHAR(50),
                preferred_court_id  INT,
                slot_start_time     VARCHAR(5) NOT NULL,
                date_offset         INT NOT NULL DEFAULT 7,
                run_dow             VARCHAR(30) NOT NULL DEFAULT '*',
                run_hour            INT NOT NULL DEFAULT 0,
                run_minute          INT NOT NULL DEFAULT 0,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hsp_booking_attempts (
                id              SERIAL PRIMARY KEY,
                job_id          INT REFERENCES hsp_booking_jobs(id) ON DELETE SET NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                status          VARCHAR(20) NOT NULL,
                message         TEXT NOT NULL DEFAULT '',
                target_date     VARCHAR(10),
                slot_start_time VARCHAR(5),
                court_id        INT,
                court_name      VARCHAR(200),
                booking_id      INT,
                raw             TEXT,
                cancelled_at    TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hsp_credentials (
                credential_id   TEXT PRIMARY KEY,
                public_key      BYTEA NOT NULL,
                sign_count      BIGINT NOT NULL DEFAULT 0,
                transports      VARCHAR(255),
                label           VARCHAR(255),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_used_at    TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hsp_consumed_tokens (
                token_hash  TEXT PRIMARY KEY,
                consumed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    conn.close()


# ── Spent enrolment tokens ───────────────────────────────────────────────────

def token_consumed(token_hash: str) -> bool:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM hsp_consumed_tokens WHERE token_hash = %s", (token_hash,))
        return cur.fetchone() is not None


def consume_token(token_hash: str) -> None:
    """Burn an enrolment token so the same link cannot be replayed."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO hsp_consumed_tokens (token_hash) VALUES (%s)
               ON CONFLICT (token_hash) DO NOTHING""",
            (token_hash,),
        )


# ── WebAuthn credentials ─────────────────────────────────────────────────────

def get_credentials() -> list[Credential]:
    with get_connection() as conn, conn.cursor(row_factory=class_row(Credential)) as cur:
        cur.execute("SELECT * FROM hsp_credentials ORDER BY created_at")
        return cur.fetchall()


def get_credential(credential_id: str) -> Credential | None:
    with get_connection() as conn, conn.cursor(row_factory=class_row(Credential)) as cur:
        cur.execute("SELECT * FROM hsp_credentials WHERE credential_id = %s", (credential_id,))
        return cur.fetchone()


def add_credential(credential_id: str, public_key: bytes, sign_count: int,
                   transports: str | None, label: str | None) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO hsp_credentials
               (credential_id, public_key, sign_count, transports, label)
               VALUES (%s, %s, %s, %s, %s)""",
            (credential_id, public_key, sign_count, transports, label),
        )


def touch_credential(credential_id: str, sign_count: int) -> None:
    """Record a successful login: bump the replay counter and the last-used stamp."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE hsp_credentials SET sign_count = %s, last_used_at = now() "
            "WHERE credential_id = %s",
            (sign_count, credential_id),
        )


def delete_credential(credential_id: str) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM hsp_credentials WHERE credential_id = %s", (credential_id,))


# ── Jobs CRUD ────────────────────────────────────────────────────────────────

def create_job(data: dict) -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO hsp_booking_jobs
               (name, enabled, activity_product_id, activity_option,
                preferred_court_id, slot_start_time, date_offset, run_dow,
                run_hour, run_minute)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                data["name"],
                data.get("enabled", True),
                data["activity_product_id"],
                data.get("activity_option"),
                data.get("preferred_court_id"),
                data["slot_start_time"],
                data.get("date_offset", 7),
                data.get("run_dow", "*"),
                data.get("run_hour", 0),
                data.get("run_minute", 0),
            ),
        )
        return cur.fetchone()["id"]


def update_job(job_id: int, data: dict) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE hsp_booking_jobs SET
               name=%s, enabled=%s, activity_product_id=%s, activity_option=%s,
               preferred_court_id=%s, slot_start_time=%s, date_offset=%s,
               run_dow=%s, run_hour=%s, run_minute=%s
               WHERE id=%s""",
            (
                data["name"],
                data.get("enabled", True),
                data["activity_product_id"],
                data.get("activity_option"),
                data.get("preferred_court_id"),
                data["slot_start_time"],
                data.get("date_offset", 7),
                data.get("run_dow", "*"),
                data.get("run_hour", 0),
                data.get("run_minute", 0),
                job_id,
            ),
        )


def get_job(job_id: int) -> Job | None:
    with get_connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        cur.execute("SELECT * FROM hsp_booking_jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def get_all_jobs() -> list[Job]:
    with get_connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        cur.execute("SELECT * FROM hsp_booking_jobs ORDER BY id")
        return cur.fetchall()


def delete_job(job_id: int) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM hsp_booking_jobs WHERE id = %s", (job_id,))


def toggle_job(job_id: int) -> bool:
    """Toggle enabled state. Returns the new state."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE hsp_booking_jobs SET enabled = NOT enabled WHERE id = %s RETURNING enabled",
            (job_id,),
        )
        row = cur.fetchone()
        return bool(row["enabled"]) if row else False


# ── Attempt history ──────────────────────────────────────────────────────────

def record_attempt(
    job_id: int | None,
    status: str,
    message: str,
    *,
    target_date: str | None = None,
    slot_start_time: str | None = None,
    court_id: int | None = None,
    court_name: str | None = None,
    booking_id: int | None = None,
    raw: str | None = None,
) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO hsp_booking_attempts
               (job_id, status, message, target_date, slot_start_time,
                court_id, court_name, booking_id, raw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (job_id, status, message, target_date, slot_start_time,
             court_id, court_name, booking_id, raw),
        )


def get_recent_attempts(limit: int = 50) -> list[Attempt]:
    with get_connection() as conn, conn.cursor(row_factory=class_row(Attempt)) as cur:
        cur.execute(
            "SELECT * FROM hsp_booking_attempts ORDER BY id DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()


def get_attempt(attempt_id: int) -> Attempt | None:
    with get_connection() as conn, conn.cursor(row_factory=class_row(Attempt)) as cur:
        cur.execute("SELECT * FROM hsp_booking_attempts WHERE id = %s", (attempt_id,))
        return cur.fetchone()


def mark_attempt_cancelled(attempt_id: int) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE hsp_booking_attempts SET cancelled_at = now() WHERE id = %s",
            (attempt_id,),
        )
