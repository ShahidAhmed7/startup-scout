"""Startup Scout on Modal: one app, one cron.

  tick            cron every 15 min (Dhaka time logic in scout/scheduler.py): pipeline, digest, reports, budget
  pipeline        the daily pipeline (spawned by tick)
  web             FastAPI: WhatsApp webhook (GET verify + POST events); later also the archive
  handle_message  async worker for one inbound WhatsApp message (spawned by web)
  ping            health check: Modal can read the secret and reach the database

  modal deploy app.py
"""
import modal

app = modal.App("startup-scout")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "psycopg[binary]==3.3.9",
        "pyyaml==6.0.3",
        "python-dotenv==1.2.4",
        "httpx==0.28.1",
        "feedparser==6.0.14",
        "trafilatura==2.3.1",
        "ddgs==9.16.1",
        "fastapi[standard]==0.165.0",
        "tzdata==2026.1",
    )
    .add_local_file("config.yaml", "/root/config.yaml")
    .add_local_file("sources.yaml", "/root/sources.yaml")
    .add_local_python_source("scout")
)

secrets = [modal.Secret.from_name("scout-secrets")]


@app.function(image=image, secrets=secrets, timeout=120)
def ping() -> dict:
    from scout.config import missing_env
    from scout.db import repo

    missing = missing_env()
    with repo.connect() as conn:
        run_id = repo.start_run(conn, "modal_ping")
        tables = repo.existing_tables(conn)
        status = "ok" if not missing and len(tables) >= len(repo.TABLES) else "error"
        repo.finish_run(conn, run_id, status, {"tables": len(tables)},
                        error=("missing settings: " + ", ".join(missing)) if missing else None)
    return {"status": status, "tables": len(tables), "missing_settings": missing}


@app.function(image=image, secrets=secrets, timeout=3600)
def pipeline() -> dict:
    from scout.db import repo
    from scout.run import run_pipeline

    with repo.connect() as conn:
        return run_pipeline(conn)


@app.function(image=image, secrets=secrets, timeout=900, scaledown_window=300)
def handle_message(wamid: str) -> None:
    from scout.db import repo
    from scout.whatsapp.commands import handle_stored

    with repo.connect() as conn:
        handle_stored(conn, wamid)


@app.function(image=image, secrets=secrets, timeout=900)
def friday_report() -> None:
    from scout.db import repo
    from scout.reports import friday

    with repo.connect() as conn:
        friday(conn)


@app.function(image=image, secrets=secrets, timeout=900)
def monthly_report() -> None:
    from scout.db import repo
    from scout.reports import monthly

    with repo.connect() as conn:
        monthly(conn)


@app.function(image=image, secrets=secrets, timeout=300, schedule=modal.Cron("*/15 * * * *", timezone="Asia/Dhaka"))
def tick() -> list:
    from scout.db import repo
    from scout.scheduler import tick as do_tick

    with repo.connect() as conn:
        return do_tick(conn, spawn={"pipeline": pipeline.spawn, "friday": friday_report.spawn,
                                    "monthly": monthly_report.spawn})


@app.function(image=image, secrets=secrets, timeout=60, scaledown_window=300)
@modal.asgi_app()
def web():
    from scout.db import repo
    from scout.whatsapp.webhook import create_app

    return create_app(spawn=lambda wamid: handle_message.spawn(wamid), connect=repo.connect)


@app.local_entrypoint()
def main():
    print(ping.remote())
