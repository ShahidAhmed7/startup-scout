"""Load owner settings (config.yaml), sources (sources.yaml) and secrets (env vars).

Locally, secrets come from the .env file. On Modal, the "scout-secrets" secret sets
the same names as environment variables. Never print secret values.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_ENV = [
    "WHATSAPP_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_BUSINESS_ACCOUNT_ID",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    "OWNER_WHATSAPP_NUMBER",
    "GEMINI_API_KEY_MAIN",
    "GEMINI_API_KEY_BACKUP",
    "DATABASE_URL",
]
OPTIONAL_ENV = ["HF_TOKEN"]

SOURCE_KINDS = {"rss", "api", "gnews", "gdelt"}
SOURCE_STATUSES = {"active", "spare", "flaky", "dead"}


def load_env() -> None:
    """Load .env into os.environ (local runs only). Existing env vars win."""
    if os.environ.get("SCOUT_NO_DOTENV") == "1":
        return
    env_file = ROOT / ".env"
    if env_file.exists():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)


def env(name: str, default: str | None = None) -> str | None:
    load_env()
    value = os.environ.get(name, default)
    return value if value else default


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing setting {name}. Add it to .env (local) or the Modal secret scout-secrets.")
    return value


def missing_env() -> list[str]:
    return [k for k in REQUIRED_ENV if not env(k)]


def dry_run() -> bool:
    return os.environ.get("DRY_RUN") == "1"


@lru_cache(maxsize=1)
def config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def sources_file() -> dict:
    with open(ROOT / "sources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sources(status: str | None = None) -> list[dict]:
    items = sources_file()["sources"]
    return [s for s in items if status is None or s["status"] == status]


def source_defaults() -> dict:
    return sources_file()["defaults"]
