"""Startup Scout on Modal.

Phase 0: one `ping` function that proves Modal can read the "scout-secrets" secret and reach
the Supabase database. Crons (07:00 pipeline, 08:00 digest, Friday report) and the WhatsApp
webhook arrive in phases 1-3. Starter plan allows 5 deployed crons; we will use at most 4.

  modal run app.py      # run ping once
  modal deploy app.py   # deploy the app
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


@app.local_entrypoint()
def main():
    print(ping.remote())
