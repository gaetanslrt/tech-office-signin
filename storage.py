"""
Storage layer for the tech office sign-in log.

Two backends, chosen automatically:

  * DATABASE_URL is set  -> PostgreSQL (Neon, Supabase, Render Postgres...).
                            Use this in production: Render's free filesystem is
                            erased on every deploy, restart and spin-down.
  * DATABASE_URL is unset -> SQLite file at DATA_PATH (default ./data/visits.db).
                            Good for running on your own machine, or on a host
                            with a real persistent disk.

Both backends expose the same functions: init(), add_visit(), sign_out(),
list_visits(), clear_visits().
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATA_PATH = os.environ.get("DATA_PATH", "data/visits.db").strip()

USING_POSTGRES = bool(DATABASE_URL)

# SQLite needs a lock: the stdlib driver is not safe across threads.
_sqlite_lock = threading.Lock()


def backend_name() -> str:
    return "postgres" if USING_POSTGRES else f"sqlite ({DATA_PATH})"


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------

def _pg_connect():
    import psycopg  # imported lazily so SQLite-only runs need no driver

    # Neon and Supabase hand out URLs starting with postgresql:// — psycopg
    # also accepts the older postgres:// spelling that some hosts still use.
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg.connect(url, connect_timeout=10)


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    left_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS visits_created_at_idx ON visits (created_at DESC);
-- Tables created before the time-out feature lack the column.
ALTER TABLE visits ADD COLUMN IF NOT EXISTS left_at TIMESTAMPTZ;
"""


def _pg_iso(value) -> str | None:
    """A Postgres timestamp as a UTC ISO string, the same shape SQLite stores."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    left_at     TEXT
);
CREATE INDEX IF NOT EXISTS visits_created_at_idx ON visits (created_at DESC);
"""


def _sqlite_connect():
    path = Path(DATA_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def init() -> None:
    """Create the table if it does not exist yet. Safe to call on every boot."""
    if USING_POSTGRES:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(_PG_SCHEMA)
            conn.commit()
    else:
        with _sqlite_lock, _sqlite_connect() as conn:
            conn.executescript(_SQLITE_SCHEMA)
            # SQLite has no ADD COLUMN IF NOT EXISTS, so look before adding.
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(visits)")}
            if "left_at" not in columns:
                conn.execute("ALTER TABLE visits ADD COLUMN left_at TEXT")
            conn.commit()


def add_visit(first_name: str, last_name: str, student_id: str, reason: str) -> dict:
    """Append one visit and return it, with the timestamp the server recorded."""
    if USING_POSTGRES:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO visits (first_name, last_name, student_id, reason)
                   VALUES (%s, %s, %s, %s)
                   RETURNING id, created_at""",
                (first_name, last_name, student_id, reason),
            )
            row = cur.fetchone()
            conn.commit()
            created = row[1]
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return {
                "id": row[0],
                "created_at": created.astimezone(timezone.utc).isoformat(),
                "first_name": first_name,
                "last_name": last_name,
                "student_id": student_id,
                "reason": reason,
                "left_at": None,
            }

    created_at = datetime.now(timezone.utc).isoformat()
    with _sqlite_lock, _sqlite_connect() as conn:
        cur = conn.execute(
            """INSERT INTO visits (created_at, first_name, last_name, student_id, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (created_at, first_name, last_name, student_id, reason),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "created_at": created_at,
            "first_name": first_name,
            "last_name": last_name,
            "student_id": student_id,
            "reason": reason,
            "left_at": None,
        }


def sign_out(student_id: str, since: datetime) -> dict | None:
    """
    Stamp the time out on this student's latest visit since `since` that has
    none yet. Returns the visit, or None when there is nothing to close.
    IDs are matched without regard to case.
    """
    if USING_POSTGRES:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE visits SET left_at = now()
                   WHERE id = (
                       SELECT id FROM visits
                       WHERE lower(student_id) = lower(%s)
                         AND left_at IS NULL
                         AND created_at >= %s
                       ORDER BY created_at DESC, id DESC
                       LIMIT 1
                       FOR UPDATE SKIP LOCKED
                   )
                   RETURNING id, created_at, first_name, last_name,
                             student_id, reason, left_at""",
                (student_id, since),
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:
                return None
            return {
                "id": row[0],
                "created_at": _pg_iso(row[1]),
                "first_name": row[2],
                "last_name": row[3],
                "student_id": row[4],
                "reason": row[5],
                "left_at": _pg_iso(row[6]),
            }

    left_at = datetime.now(timezone.utc).isoformat()
    with _sqlite_lock, _sqlite_connect() as conn:
        row = conn.execute(
            """SELECT id, created_at, first_name, last_name, student_id, reason
               FROM visits
               WHERE lower(student_id) = lower(?)
                 AND left_at IS NULL
                 AND created_at >= ?
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (student_id, since.astimezone(timezone.utc).isoformat()),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE visits SET left_at = ? WHERE id = ?", (left_at, row["id"]))
        conn.commit()
        return {**dict(row), "left_at": left_at}


def clear_visits() -> int:
    """Delete every visit. Returns how many rows were removed."""
    if USING_POSTGRES:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM visits")
            count = cur.rowcount
            conn.commit()
            return count

    with _sqlite_lock, _sqlite_connect() as conn:
        count = conn.execute("DELETE FROM visits").rowcount
        conn.commit()
        return count


def list_visits(limit: int | None = None) -> list[dict]:
    """Every visit, newest first."""
    if USING_POSTGRES:
        sql = """SELECT id, created_at, first_name, last_name, student_id, reason, left_at
                 FROM visits ORDER BY created_at DESC, id DESC"""
        params: tuple = ()
        if limit:
            sql += " LIMIT %s"
            params = (limit,)
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "id": r[0],
                    "created_at": _pg_iso(r[1]),
                    "first_name": r[2],
                    "last_name": r[3],
                    "student_id": r[4],
                    "reason": r[5],
                    "left_at": _pg_iso(r[6]),
                }
                for r in cur.fetchall()
            ]

    sql = """SELECT id, created_at, first_name, last_name, student_id, reason, left_at
             FROM visits ORDER BY created_at DESC, id DESC"""
    params = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    with _sqlite_lock, _sqlite_connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
