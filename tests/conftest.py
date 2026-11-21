import pytest

FAKE_OWNER = "8801700000000"


@pytest.fixture(autouse=True)
def safe_env(monkeypatch):
    """Tests never load the real .env, never send real messages."""
    monkeypatch.setenv("SCOUT_NO_DOTENV", "1")
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("OWNER_WHATSAPP_NUMBER", FAKE_OWNER)
    monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("GEMINI_API_KEY_MAIN", "main-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "backup-key")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    # Unit tests must never wake the real GPU: the readiness check is a no-op here.
    from scout.llm import guard

    async def ready(base_url, deadline_s):
        return None

    monkeypatch.setattr(guard, "wait_until_ready", ready)
