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
        "python-multipart==0.0.34",
        "pydantic-ai-slim[google,openai]==2.71.0",
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
        repo.finish_run(
            conn,
            run_id,
            status,
            {"tables": len(tables)},
            error=("missing settings: " + ", ".join(missing)) if missing else None,
        )
    return {"status": status, "tables": len(tables), "missing_settings": missing}


@app.function(image=image, secrets=secrets, timeout=3600)
def pipeline() -> dict:
    from scout.db import repo
    from scout.run import run_pipeline

    with repo.connect() as conn:
        return run_pipeline(conn)


@app.function(image=image, secrets=secrets, timeout=2400, scaledown_window=300)
def handle_message(wamid: str) -> None:
    from scout.db import repo
    from scout.whatsapp.commands import handle_stored

    with repo.connect() as conn:
        handle_stored(conn, wamid)


@app.function(image=image, secrets=secrets, timeout=2400)
def friday_report() -> None:
    from scout.db import repo
    from scout.reports import friday

    with repo.connect() as conn:
        friday(conn)


@app.function(image=image, secrets=secrets, timeout=2400)
def monthly_report() -> None:
    from scout.db import repo
    from scout.reports import monthly

    with repo.connect() as conn:
        monthly(conn)


@app.function(
    image=image,
    secrets=secrets,
    timeout=300,
    schedule=modal.Cron("*/15 * * * *", timezone="Asia/Dhaka"),
)
def tick() -> list:
    from scout.db import repo
    from scout.scheduler import tick as do_tick

    with repo.connect() as conn:
        return do_tick(
            conn,
            spawn={
                "pipeline": pipeline.spawn,
                "friday": friday_report.spawn,
                "monthly": monthly_report.spawn,
            },
        )


@app.function(image=image, secrets=secrets, timeout=60, scaledown_window=300)
@modal.asgi_app()
def web():
    from scout.db import repo
    from scout.whatsapp.webhook import create_app

    return create_app(
        spawn=lambda wamid: handle_message.spawn(wamid), connect=repo.connect
    )


# ---------------------------------------------------------------- Qwen backup / private model (phase 3)

QWEN_REPO = "Qwen/Qwen3.6-27B-FP8"  # Apache 2.0; FP8 fits one A100 80GB (weight-only FP8 via Marlin on Ampere)
QWEN_DIR = "/models/Qwen3.6-27B-FP8"
weights = modal.Volume.from_name("scout-qwen-weights", create_if_missing=True)

download_image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "huggingface_hub==2.3.0"
)
# NVIDIA's CUDA "devel" image includes nvcc: vLLM's FlashInfer kernels compile on first use and fail without it.
qwen_image = (
    modal.Image.from_registry("nvidia/cuda:13.0.2-devel-ubuntu24.04", add_python="3.12")
    .entrypoint([])
    .apt_install("ninja-build")
    .pip_install("vllm==0.33.0", "httpx==0.28.1")
    .env(
        {
            "VLLM_LOGGING_LEVEL": "WARNING",
            "HF_HUB_OFFLINE": "1",
            "CUDA_HOME": "/usr/local/cuda",
            # keep compiled kernels on the volume so later cold starts skip the ~3 min compile
            "FLASHINFER_WORKSPACE_BASE": "/models/.cache/flashinfer",
            "VLLM_CACHE_ROOT": "/models/.cache/vllm",
            "TRITON_CACHE_DIR": "/models/.cache/triton",
        }
    )
)


@app.function(
    image=download_image,
    volumes={"/models": weights},
    secrets=secrets,
    timeout=3600,
    cpu=2,
)
def download_qwen() -> dict:
    """Download the weights once into the Volume (CPU only, so no GPU time is spent on downloading)."""
    import os

    from huggingface_hub import snapshot_download

    snapshot_download(
        QWEN_REPO, local_dir=QWEN_DIR, token=os.environ.get("HF_TOKEN") or None
    )
    weights.commit()
    size = sum(
        os.path.getsize(os.path.join(d, f))
        for d, _, fs in os.walk(QWEN_DIR)
        for f in fs
    )
    return {"repo": QWEN_REPO, "gb": round(size / 1e9, 1)}


@app.cls(
    image=qwen_image.add_local_python_source("scout"),
    gpu="A100-80GB",
    volumes={"/models": weights},
    timeout=1800,
    scaledown_window=120,
    max_containers=1,
    retries=0,
    secrets=secrets,
)
class Qwen:
    @modal.web_server(8000, startup_timeout=1200)
    def serve(self):
        import os
        import subprocess

        os.environ["VLLM_API_KEY"] = os.environ["QWEN_API_KEY"]
        self.proc = subprocess.Popen(
            [
                "vllm",
                "serve",
                QWEN_DIR,
                "--served-model-name",
                QWEN_REPO,
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--max-model-len",
                "65536",
                "--gpu-memory-utilization",
                "0.92",
                "--reasoning-parser",
                "qwen3",
                "--enforce-eager",
                "--disable-log-requests",
                "--middleware",
                "scout.llm.private_server.BearerGate",
            ]
        )

    @modal.exit()
    def stop(self):
        if hasattr(self, "proc"):
            self.proc.terminate()
        try:
            weights.commit()
        except Exception:
            pass


@app.local_entrypoint()
def main():
    print(ping.remote())
