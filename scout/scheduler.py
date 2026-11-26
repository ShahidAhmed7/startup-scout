"""The 15-minute tick (one Modal cron instead of four, so `time 7:30` works and we stay under the 5-cron limit).

Each tick decides, in Dhaka time:
- pipeline:  from 1 hour before digest_time, once a day (one retry if it failed). If Google could not be reached,
             retry every 30 minutes until 13:00 (config `pipeline_retry`); the digest then arrives late;
- digest:    at digest_time, send the prepared digest if the 24-hour window is open, else mark it pending
             (a digest finished later goes out at the next tick); if Google is down, one "running late" note;
             if the pipeline failed for good and nothing is ready, a short "skipped today" note;
- friday:    Fridays from 09:00, once;
- monthly:   the 1st of the month from 09:00, once;
- budget:    the $20 warning, once a month.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from scout.settings import effective
from scout.whatsapp import outbox

DHAKA = ZoneInfo("Asia/Dhaka")
PIPELINE_LEAD = timedelta(minutes=60)
# Modal stops the pipeline function after 60 minutes, leaving its run marked "running".
STUCK_AFTER = timedelta(minutes=70)
GOOGLE_DOWN = "GoogleUnreachable"


def _runs_today(conn, kind: str, day) -> list[dict]:
    return conn.execute(
        """select * from runs where kind = %s and (started_at at time zone 'Asia/Dhaka')::date = %s
           order by started_at desc""", (kind, day)).fetchall()


def _close_stuck_runs(conn, now: datetime) -> None:
    conn.execute(
        """update runs set status = 'error', finished_at = now(), error = coalesce(error, 'Stopped')
           where kind = 'pipeline' and status = 'running' and started_at < %s""", (now - STUCK_AFTER,))


def _retry_window(cfg: dict, digest_at: datetime) -> tuple[datetime, timedelta, int]:
    r = cfg.get("pipeline_retry") or {}
    hh, mm = (int(x) for x in str(r.get("until", "13:00")).split(":"))
    until = max(digest_at.replace(hour=hh, minute=mm), digest_at + timedelta(hours=1))
    return until, timedelta(minutes=r.get("every_minutes", 30)), r.get("max_attempts", 10)


def _pipeline_state(conn, day, now: datetime, cfg: dict | None = None, digest_at: datetime | None = None) -> str:
    """ok | running | retry (start one now) | waiting (Google down; next try later) | failed (give up today)."""
    runs = _runs_today(conn, "pipeline", day)
    if any(r["status"] == "ok" for r in runs):
        return "ok"
    if any(r["status"] == "running" and now - r["started_at"] < STUCK_AFTER for r in runs):
        return "running"
    if runs and runs[0].get("error") == GOOGLE_DOWN and cfg is not None and digest_at is not None:
        until, every, max_attempts = _retry_window(cfg, digest_at)
        if now >= until or len(runs) >= max_attempts:
            return "failed"
        last_end = runs[0].get("finished_at") or runs[0]["started_at"]
        return "retry" if now - last_end >= every else "waiting"
    return "retry" if len(runs) < 2 else "failed"


def _google_down_today(conn, day) -> bool:
    return any(r.get("error") == GOOGLE_DOWN for r in _runs_today(conn, "pipeline", day))


def _note_once(conn, kind: str, day, body: str) -> bool:
    """Send a short note at most once a day (remembered as a run row)."""
    from scout.db import repo

    if _runs_today(conn, kind, day):
        return False
    rid = repo.start_run(conn, kind)
    outbox.deliver(conn, outbox.text(body), kind)
    repo.finish_run(conn, rid, "ok", {})
    return True


def tick(conn, now: datetime | None = None, spawn: dict[str, Callable[[], None]] | None = None) -> list[str]:
    spawn = spawn or {}
    now = (now or datetime.now(DHAKA)).astimezone(DHAKA)
    today = now.date()
    cfg = effective(conn)
    hh, mm = (int(x) for x in cfg["digest_time"].split(":"))
    digest_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    actions: list[str] = []

    _close_stuck_runs(conn, now)
    state = _pipeline_state(conn, today, now, cfg, digest_at)
    if now >= digest_at - PIPELINE_LEAD and state == "retry" and "pipeline" in spawn:
        spawn["pipeline"]()
        actions.append("pipeline started")
        state = "running"

    if now >= digest_at:
        d = conn.execute("select * from digests where date = %s", (today,)).fetchone()
        if d and d["status"] == "prepared":
            if outbox.window_open(conn):
                outbox.send_digest(conn, d)
                actions.append("digest sent")
            else:
                conn.execute("update digests set status = 'pending' where id = %s", (d["id"],))
                actions.append("digest pending (window closed)")
        elif d is None and state == "failed":
            conn.execute("insert into digests (date, status, body_json) values (%s, 'skipped', '{}') "
                         "on conflict (date) do nothing", (today,))
            if _google_down_today(conn, today):
                note = ("Skipped today: Google's AI service couldn't be reached from our server all morning. "
                        "Today's news will be in tomorrow's digest.")
            else:
                note = "Skipped today: the AI models were unavailable. The rest is coming tomorrow."
            outbox.deliver(conn, outbox.text(note), "skipped_note")
            actions.append("skipped note")
        elif d is None and state in ("retry", "waiting", "running") and _google_down_today(conn, today):
            until, _, _ = _retry_window(cfg, digest_at)
            if _note_once(conn, "late_note", today,
                          "Your digest is late today: Google's AI service isn't answering our server right now. "
                          f"I'll keep trying until {until:%H:%M} and send it as soon as it's ready."):
                actions.append("late note")

    if now.weekday() == 4 and now.hour >= 9 and cfg["reports"].get("friday", True) \
            and not _runs_today(conn, "friday", today) and "friday" in spawn:
        spawn["friday"]()
        actions.append("friday report started")

    if now.day == 1 and now.hour >= 9 and cfg["reports"].get("monthly", True) \
            and not _runs_today(conn, "monthly", today) and "monthly" in spawn:
        spawn["monthly"]()
        actions.append("monthly report started")

    try:
        from scout import budget

        msg = budget.warning_due(conn)
        if msg:
            outbox.deliver(conn, outbox.text(msg), "budget_warning")
            actions.append("budget warning")
    except ImportError:
        pass
    return actions
