"""Budget guard (CLAUDE.md section 6). Modal's free Starter plan gives $30/month; our ceiling is $25.

Estimate = Qwen GPU seconds this month x A100-80GB price + a small CPU allowance.
- At $20: warn the owner once (queued until the 24-hour window is open).
- At $25: no GPU until the 1st; Gemini only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scout.config import config
from scout.db import repo


def month_spend(conn) -> float:
    b = config()["budget"]
    return round(repo.gpu_seconds_this_month(conn) * b["a100_80gb_usd_per_second"] + b["cpu_allowance_usd_per_month"], 2)


def gpu_blocked(conn) -> bool:
    return month_spend(conn) >= config()["budget"]["modal_stop_usd"]


def warning_due(conn) -> str | None:
    """The $20 warning text, once per month (remembered in `settings`), else None."""
    b = config()["budget"]
    spend = month_spend(conn)
    if spend < b["modal_warn_usd"]:
        return None
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute("select value from settings where key = 'budget_warned_month'").fetchone()
    if row and row["value"] == month:
        return None
    conn.execute("""insert into settings (key, value) values ('budget_warned_month', to_jsonb(%s::text))
                    on conflict (key) do update set value = excluded.value, updated_at = now()""", (month,))
    return (f"Budget note: Modal spend this month is about ${spend:.2f} of the ${b['modal_stop_usd']} limit. "
            f"At ${b['modal_stop_usd']} the backup model pauses until the 1st and only Gemini is used.")
