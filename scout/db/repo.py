"""Database access: plain SQL with psycopg 3."""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from scout.config import require_env

SCHEMA = "scout"
TABLES = [
    "sources", "items", "startups", "rounds", "scores", "skipped", "cards", "digests",
    "messages", "owner_state", "settings", "ideas", "notes", "assumptions", "watches",
    "signals", "usage", "runs", "yc_seen",
]
PACIFIC = ZoneInfo("America/Los_Angeles")


def connect(autocommit: bool = True) -> psycopg.Connection:
    # prepare_threshold=None: no server-side prepared statements, which keeps the
    # Supabase pooler happy in both session and transaction mode.
    conn = psycopg.connect(
        require_env("DATABASE_URL"),
        autocommit=autocommit,
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=15,
        application_name="startup-scout",
    )
    conn.execute(f"set search_path to {SCHEMA}, public")
    return conn


def existing_tables(conn) -> list[str]:
    rows = conn.execute(
        "select tablename from pg_tables where schemaname = %s order by tablename", (SCHEMA,)
    ).fetchall()
    return [r["tablename"] for r in rows]


# ---------- runs ----------

def start_run(conn, kind: str) -> int:
    return conn.execute("insert into runs (kind) values (%s) returning id", (kind,)).fetchone()["id"]


def finish_run(conn, run_id: int, status: str, stats: dict | None = None, error: str | None = None) -> None:
    conn.execute(
        "update runs set finished_at = now(), status = %s, stats_json = %s, error = %s where id = %s",
        (status, json.dumps(stats or {}), error, run_id),
    )


def last_run(conn, kind: str) -> dict | None:
    return conn.execute(
        "select * from runs where kind = %s order by started_at desc limit 1", (kind,)
    ).fetchone()


# ---------- usage ----------

def record_usage(conn, *, provider: str, model_id: str | None, key_label: str | None, kind: str,
                 status: str, tokens_in: int | None = None, tokens_out: int | None = None,
                 gpu_seconds: float | None = None) -> None:
    conn.execute(
        """insert into usage (provider, model_id, key_label, kind, tokens_in, tokens_out, gpu_seconds, status)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (provider, model_id, key_label, kind, tokens_in, tokens_out, gpu_seconds, status),
    )


def pacific_day_start(now: datetime | None = None) -> datetime:
    """Gemini's free daily quota resets at midnight Pacific time."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(PACIFIC)
    return datetime.combine(local.date(), time(0), tzinfo=PACIFIC)


def gemini_calls_today(conn, model_id: str, key_label: str) -> int:
    """Calls that reached Google today (Pacific day). Rejected 429s and our own cap blocks don't count."""
    row = conn.execute(
        """select count(*) as n from usage
           where provider = 'gemini' and model_id = %s and key_label = %s
             and status not in ('quota', 'cap', 'network') and created_at >= %s""",
        (model_id, key_label, pacific_day_start()),
    ).fetchone()
    return row["n"]


def gpu_seconds_this_month(conn) -> float:
    row = conn.execute(
        """select coalesce(sum(gpu_seconds), 0) as s from usage
           where provider = 'qwen' and created_at >= date_trunc('month', now())"""
    ).fetchone()
    return float(row["s"])


# ---------- messages ----------

def log_message(conn, *, direction: str, wa_message_id: str | None, type_: str, body: str) -> bool:
    """Store a message. Returns False if this WhatsApp id was already stored (idempotency)."""
    row = conn.execute(
        """insert into messages (direction, wa_message_id, type, body) values (%s, %s, %s, %s)
           on conflict (wa_message_id) do nothing returning id""",
        (direction, wa_message_id, type_, body),
    ).fetchone()
    return row is not None
