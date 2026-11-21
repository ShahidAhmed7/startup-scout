"""Health check: python -m scout.status

Setup checks are free: no Gemini quota is used and no WhatsApp message is sent.
"""

from __future__ import annotations

import json
import subprocess
import sys

from scout.config import ROOT, config, missing_env, sources

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


def line(ok: bool | None, label: str, detail: str = "") -> bool:
    mark = (
        f"{GREEN}✓{RESET}"
        if ok
        else (f"{YELLOW}!{RESET}" if ok is None else f"{RED}✗{RESET}")
    )
    print(f" {mark} {label:<26} {DIM if ok else ''}{detail}{RESET}")
    return bool(ok)


def check(label: str, fn) -> bool:
    try:
        ok, detail = fn()
    except (
        Exception
    ) as e:  # never print secrets: only the error type and a short message
        ok, detail = False, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
    return line(ok, label, detail)


# ---------- individual checks ----------


def c_files():
    cfg = config()
    act = len(sources("active"))
    dead = len(sources("dead")) + len(sources("flaky"))
    return (
        True,
        f"{act} active sources ({dead} dead or flaky); {len(cfg['models'])} configured model entries",
    )


def c_env():
    miss = missing_env()
    ignored = ".env" in (ROOT / ".gitignore").read_text().split()
    if miss:
        return False, "missing: " + ", ".join(miss)
    return (
        ignored,
        "all present; .env is in .gitignore"
        if ignored
        else ".env is NOT in .gitignore",
    )


def c_db():
    from scout.db import repo

    with repo.connect() as conn:
        have = repo.existing_tables(conn)
    miss = [t for t in repo.TABLES if t not in have]
    return (not miss), f"{len(have)} tables" + (
        f", missing: {', '.join(miss)} (run python -m scout.db.migrate)" if miss else ""
    )


def c_model_entry(entry):
    def fn():
        from scout.llm.registry import check_entry

        result = check_entry(entry)
        return result in (
            "ok",
            "configured (GPU not started)",
            "configured (provider has no free metadata check)",
        ), result

    return fn


def c_whatsapp_token():
    from scout.whatsapp.client import check_phone

    info = check_phone()
    return (
        info.get("quality_rating") != "RED",
        f"{info.get('verified_name')} {info.get('display_phone_number')}, quality {info.get('quality_rating')}",
    )


def c_whatsapp_test():
    from scout.db import repo

    with repo.connect() as conn:
        row = conn.execute(
            "select created_at from messages where direction = 'out' and type = 'test' order by created_at desc limit 1"
        ).fetchone()
    if row:
        return (
            True,
            f"test message sent {row['created_at']:%Y-%m-%d %H:%M} UTC (check your phone)",
        )
    return (
        False,
        "not sent yet: send 'hi' to the bot, then run python -m scout.smoke whatsapp --owner-said-hi",
    )


MODAL = [sys.executable, "-m", "modal"]  # the project's own Modal install


def _modal_json(*args) -> list:
    out = subprocess.run(
        [*MODAL, *args, "--json"], capture_output=True, text=True, timeout=60
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip().splitlines()[-1][:160])
    return json.loads(out.stdout or "[]")


def c_modal_login():
    out = subprocess.run(
        [*MODAL, "profile", "current"], capture_output=True, text=True, timeout=30
    )
    return (
        out.returncode == 0,
        f"profile {out.stdout.strip()}" if out.returncode == 0 else "run: modal setup",
    )


def c_modal_secret():
    names = {(s.get("Name") or s.get("name")) for s in _modal_json("secret", "list")}
    return (
        "scout-secrets" in names,
        "scout-secrets exists"
        if "scout-secrets" in names
        else "missing: scout-secrets",
    )


def c_modal_db():
    from scout.db import repo

    with repo.connect() as conn:
        run = repo.last_run(conn, "modal_ping")
    if not run:
        return False, "not checked yet: run modal run app.py"
    ok = run["status"] == "ok"
    return (
        ok,
        f"Modal reached the database at {run['started_at']:%Y-%m-%d %H:%M} UTC"
        if ok
        else f"last ping failed: {run['error']}",
    )


# ---------- daily numbers ----------


def daily_report() -> None:
    from scout.db import repo

    cfg = config()
    print("\nToday")
    try:
        with repo.connect() as conn:
            run = repo.last_run(conn, "pipeline")
            if run:
                s = run["stats_json"] or {}
                print(
                    f"   Last pipeline run      {run['started_at']:%Y-%m-%d %H:%M} UTC, {run['status']}"
                )
                print(f"   Items collected        {s.get('items', 'unknown')}")
                print(f"   Cards made             {s.get('cards', 'unknown')}")
            else:
                print("   Last pipeline run      none yet (phase 1)")
            for entry, mc in cfg["models"].items():
                if "cap_per_day" not in mc:
                    continue
                n = repo.gemini_calls_today(conn, mc["model"].split(":", 1)[-1], entry)
                print(f"   {entry:<22} {n}/{mc['cap_per_day']} today (Pacific day)")
            gpu = repo.gpu_seconds_this_month(conn)
            b = cfg["budget"]
            spend = (
                gpu * b["a100_80gb_usd_per_second"] + b["cpu_allowance_usd_per_month"]
            )
            print(
                f"   Modal spend this month ~${spend:.2f} (GPU {gpu / 3600:.2f} h + ${b['cpu_allowance_usd_per_month']:.2f} CPU allowance; warn ${b['modal_warn_usd']}, stop ${b['modal_stop_usd']})"
            )
            down = conn.execute(
                "select name from sources where status = 'active' and fail_count >= 3 order by name"
            ).fetchall()
            print(
                f"   Sources down           {', '.join(r['name'] for r in down) if down else 'none'}"
            )
            au = repo.last_run(conn, "audit")
            if au:
                sm = (au["stats_json"] or {}).get("summary", {})
                print(
                    f"   Last quality audit     {au['started_at']:%Y-%m-%d %H:%M} UTC: "
                    + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sm.items())
                )
            from datetime import datetime, timezone

            from scout import firecrawl

            if firecrawl.available():
                month = datetime.now(timezone.utc).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                )
                print(
                    f"   Firecrawl credits      {firecrawl.credits_used(conn, month)}/{cfg['firecrawl']['monthly_credit_cap']} "
                    f"this month (free plan: 1,000)"
                )
            else:
                print(
                    "   Firecrawl              not set up (add FIRECRAWL_API_KEY to read blocked sites)"
                )
    except Exception as e:
        print(f"   (could not read the database: {type(e).__name__})")


def main() -> int:
    print("Setup")
    results = [
        check("Settings files", c_files),
        check("Secrets (.env)", c_env),
        check("Database (Supabase)", c_db),
        *[
            check("Model " + entry, c_model_entry(entry))
            for entry in config()["models"]
        ],
        check("WhatsApp token", c_whatsapp_token),
        check("WhatsApp test message", c_whatsapp_test),
        check("Modal login", c_modal_login),
        check("Modal secret", c_modal_secret),
        check("Modal → database", c_modal_db),
    ]
    daily_report()
    ok = all(results)
    print(
        f"\n{GREEN}All green.{RESET}"
        if ok
        else f"\n{RED}{results.count(False)} check(s) need attention.{RESET}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
