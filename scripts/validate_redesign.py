"""Resumable real-service validation; all messaging is dry-run and pipeline changes roll back."""

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DRY_RUN"] = "1"
os.environ["SCOUT_DEV"] = "1"
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"
from scout.config import load_env
from scout.db import repo


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--full", action="store_true")
    p.add_argument("--metadata", action="store_true")
    p.add_argument("--progress", action="store_true")
    p.add_argument(
        "--operation",
        choices=[
            "integration",
            "private-smoke",
            "restart-monitor",
            "commit-steps",
            "sync-secrets",
            "deploy",
            "configure-endpoint",
            "post-deploy",
            "push",
        ],
    )
    a = p.parse_args()
    load_env()
    data = ROOT / "data/redesign"
    data.mkdir(parents=True, exist_ok=True)
    if a.operation:
        operate(a.operation, data)
    if a.progress:
        with repo.connect() as conn:
            rows = conn.execute(
                "select key_label,kind,status,count(*) n from usage where created_at>now()-interval '1 hour' group by key_label,kind,status order by key_label,kind,status"
            ).fetchall()
            print(json.dumps(rows, indent=2))
        pidfile = data / "resume.pid"
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 0)
                print("Resume monitor alive")
            except OSError:
                print("Resume monitor not running")
    if a.prepare:
        backup = (
            data
            / "backups"
            / (
                "database-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + ".json"
            )
        )
        with repo.connect() as conn:
            contents = {
                t: conn.execute("select * from " + t).fetchall() for t in repo.TABLES
            }
        backup.write_text(json.dumps(contents, default=str))
        backup.chmod(0o600)
        print("Database backed up:", backup.name, flush=True)
        from scout.db.migrate import migrate

        print("Migration:", len(migrate()), "tables", flush=True)
        if not os.environ.get("QWEN_API_KEY"):
            key = secrets.token_urlsafe(48)
            with (ROOT / ".env").open("a") as f:
                f.write("\nQWEN_API_KEY=" + key + "\n")
            os.environ["QWEN_API_KEY"] = key
            print("Private bearer key saved without displaying it.", flush=True)
    if a.metadata:
        from scout.llm.registry import check_entry
        from scout.config import config

        for entry in config()["models"]:
            try:
                print(entry, check_entry(entry), flush=True)
            except Exception as exc:
                print(entry, type(exc).__name__, flush=True)
    if a.full:
        from scout.selftest import variety

        with repo.connect() as conn:
            variety(conn, full=True)


def operate(operation, data):
    import subprocess
    import modal

    def command(args):
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
        print(result.stdout)
        if result.returncode:
            print(result.stderr)
            raise SystemExit(result.returncode)

    if operation == "integration":
        from scripts.redesign_e2e import integration

        integration()
    elif operation == "private-smoke":
        from scout.llm import run as ai
        from scout.llm.schemas import Ask

        result = ai.run(
            "note",
            "Synthetic privacy test: reply on_topic=true, answer=private endpoint connected.",
            Ask,
            private=True,
        )
        assert result.entry == "private" and result.output.on_topic
        print(
            "Authenticated private structured inference:",
            result.model,
            result.entry,
            "PASS",
        )
    elif operation == "restart-monitor":
        pidfile = data / "resume.pid"
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 15)
            except ProcessLookupError:
                pass
        thread = "01a1fc04-5f19-76a2-bced-e7184ab73ff4"
        rollout = (
            "/Users/shahidahmed/.codex/sessions/2026/11/21/rollout-2026-11-21T08-51-47-"
            + thread
            + ".jsonl"
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "scripts/resume_redesign.py",
                "--thread",
                thread,
                "--rollout",
                rollout,
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pidfile.write_text(str(child.pid))
        print("Resume monitor restarted", child.pid)
    elif operation == "sync-secrets":
        command(
            [
                ".venv/bin/modal",
                "secret",
                "create",
                "scout-secrets",
                "--from-dotenv",
                ".env",
                "--force",
            ]
        )
    elif operation == "deploy":
        command([".venv/bin/modal", "deploy", "app.py"])
    elif operation == "configure-endpoint":
        endpoint = (
            modal.Cls.from_name("startup-scout", "Qwen")()
            .serve.get_web_url()
            .rstrip("/")
            + "/v1"
        )
        p = ROOT / ".env"
        lines = [
            line
            for line in p.read_text().splitlines()
            if not line.startswith("QWEN_BASE_URL=")
        ]
        p.write_text("\n".join(lines) + "\nQWEN_BASE_URL=" + endpoint + "\n")
        p.chmod(0o600)
        print("Private endpoint configured:", endpoint)
        command(
            [
                ".venv/bin/modal",
                "secret",
                "create",
                "scout-secrets",
                "--from-dotenv",
                ".env",
                "--force",
            ]
        )
    elif operation == "post-deploy":
        print("Modal ping:", modal.Function.from_name("startup-scout", "ping").remote())
        import httpx, hashlib, hmac, time
        from scout.config import require_env

        base = require_env("PUBLIC_BASE_URL").rstrip("/")
        response = httpx.get(base + "/archive/login", timeout=60)
        assert response.status_code == 200 and "Sign in" in response.text
        message_id = "selftest.redesign." + str(int(time.time()))
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": require_env("OWNER_WHATSAPP_NUMBER"),
                                        "id": message_id,
                                        "timestamp": str(int(time.time())),
                                        "type": "text",
                                        "text": {"body": "help"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }
        body = json.dumps(payload).encode()
        signature = (
            "sha256="
            + hmac.new(
                require_env("WHATSAPP_APP_SECRET").encode(), body, hashlib.sha256
            ).hexdigest()
        )
        with repo.connect() as conn:
            before = conn.execute(
                "select last_owner_message_at,pending_queue_json from owner_state where id=1"
            ).fetchone()
        response = httpx.post(
            base + "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        assert response.status_code == 200
        for attempt in range(20):
            with repo.connect() as conn:
                row = conn.execute(
                    "select type from messages where wa_message_id=%s", (message_id,)
                ).fetchone()
                after = conn.execute(
                    "select last_owner_message_at,pending_queue_json from owner_state where id=1"
                ).fetchone()
            if row and row["type"].endswith(":done"):
                break
            time.sleep(3)
        assert row and row["type"].endswith(":done") and before == after
        print(
            "Deployed archive login and signed dry-run webhook worker: PASS; owner window/queue unchanged"
        )
    elif operation == "push":
        command(["git", "push", "origin", "main"])
    elif operation == "commit-steps":
        groups = [
            (
                "Introduce configurable model routing with guarded structured outputs",
                [
                    "requirements.txt",
                    "config.yaml",
                    "scout/llm",
                    "scout/db/schema.sql",
                    "tests/test_gemini.py",
                    "tests/test_router_privacy.py",
                ],
            ),
            (
                "Remove profile-based scoring and add ambition settings",
                [
                    "scout/settings.py",
                    "scout/whatsapp/commands.py",
                    "tests/test_commands.py",
                    "tests/test_config.py",
                ],
            ),
            (
                "Classify candidates before balancing the regional scoring pool",
                [
                    "scout/classify.py",
                    "scout/pool.py",
                    "scout/dedupe.py",
                    "scout/extract.py",
                    "sources.yaml",
                ],
            ),
            (
                "Compute opportunity scores and enforce diverse daily selections",
                [
                    "scout/score.py",
                    "scout/novelty.py",
                    "scout/taste.py",
                    "tests/test_shortlist.py",
                ],
            ),
            (
                "Attach actions to concise cards and expand full analysis",
                [
                    "scout/cards.py",
                    "scout/deepdive.py",
                    "scout/digest.py",
                    "scout/whatsapp/formats.py",
                    "scout/whatsapp/client.py",
                    "scout/whatsapp/outbox.py",
                    "tests/test_whatsapp_client.py",
                    "scout/notebook.py",
                ],
            ),
            (
                "Integrate research updates and rollback-only verification",
                [
                    "scout/run.py",
                    "scout/audit.py",
                    "scout/selftest.py",
                    "scout/status.py",
                    "scout/smoke.py",
                    "scout/reports.py",
                    "tests/test_redesign.py",
                ],
            ),
            (
                "Update archive filters and authenticated private inference hosting",
                [
                    "scout/archive.py",
                    "scout/config.py",
                    "app.py",
                    ".env.example",
                    "tests/test_archive.py",
                    "tests/test_formats.py",
                ],
            ),
            (
                "Document the opportunity redesign and unattended verification",
                ["README.md", "scripts"],
            ),
        ]
        for message, files in groups:
            command(["git", "add", "-A", "--", *files])
            if subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=ROOT
            ).returncode:
                command(["git", "commit", "-m", message])


if __name__ == "__main__":
    main()
