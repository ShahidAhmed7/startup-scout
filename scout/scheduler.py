"""The 15-minute tick (one Modal cron instead of four, so `time 7:30` works and we stay under the 5-cron limit).

Each tick decides, in Dhaka time:
- pipeline:  from 1 hour before digest_time, once a day (one retry if it failed);
- digest:    at digest_time, send the prepared digest if the 24-hour window is open, else mark it pending;
             if the pipeline failed and nothing is ready, queue a short "skipped today" note;
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
STUCK_AFTER = timedelta(hours=2)


def _runs_today(conn, kind: str, day) -> list[dict]:
    return conn.execute(
        """select * from runs where kind = %s and (started_at at time zone 'Asia/Dhaka')::date = %s
           order by started_at desc""", (kind, day)).fetchall()


def _pipeline_state(conn, day, now: datetime) -> str:
    runs = _runs_today(conn, "pipeline", day)
    if any(r["status"] == "ok" for r in runs):
        return "ok"
    if any(r["status"] == "running" and now - r["started_at"] < STUCK_AFTER for r in runs):
        return "running"
    return "retry" if len(runs) < 2 else "failed"


def tick(conn, now: datetime | None = None, spawn: dict[str, Callable[[], None]] | None = None) -> list[str]:
    spawn = spawn or {}
    now = (now or datetime.now(DHAKA)).astimezone(DHAKA)
    today = now.date()
    cfg = effective(conn)
    hh, mm = (int(x) for x in cfg["digest_time"].split(":"))
    digest_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    actions: list[str] = []

    state = _pipeline_state(conn, today, now)
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
            outbox.deliver(conn, outbox.text("Skipped today: the AI models were unavailable. "
                                             "The rest is coming tomorrow."), "skipped_note")
            actions.append("skipped note")

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
