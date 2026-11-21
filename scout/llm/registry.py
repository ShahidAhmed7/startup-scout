"""Config-driven Pydantic AI models; credentials never enter prompts or logs."""

from functools import lru_cache
from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from scout.config import config, require_env


def settings(entry):
    raw = config()["models"][entry].get("settings", {})
    result = {
        k: v
        for k, v in raw.items()
        if k not in ("max_output_tokens", "timeout_s", "thinking")
    }
    if "max_output_tokens" in raw:
        result["max_tokens"] = raw["max_output_tokens"]
    result["timeout"] = raw.get("timeout_s", 300)
    result["max_retries"] = 0  # every real attempt must pass our guard
    if "thinking" in raw:
        if config()["models"][entry]["model"].startswith("google:"):
            result["google_thinking_config"] = {
                "thinking_level": raw["thinking"].upper()
            }
        else:
            result["thinking"] = raw["thinking"]
    return result


@lru_cache(maxsize=None)
def get_model(entry):
    cfg = config()["models"][entry]
    provider, model = cfg["model"].split(":", 1)
    key = require_env(cfg["key_env"]) if cfg.get("key_env") else None
    base = require_env(cfg["base_url_env"]) if cfg.get("base_url_env") else None
    if provider == "google":
        return GoogleModel(model, provider=GoogleProvider(api_key=key, base_url=base))
    if provider == "openai-chat":
        return OpenAIChatModel(
            model, provider=OpenAIProvider(api_key=key, base_url=base)
        )

    # Pydantic AI supports additional providers without routing changes.
    def factory(name):
        from pydantic_ai.providers import infer_provider_class

        cls = infer_provider_class(name)
        return cls(
            **({"api_key": key} if key else {}), **({"base_url": base} if base else {})
        )

    return infer_model(cfg["model"], provider_factory=factory)


def check_entry(entry):
    """Metadata only. Never starts a scale-to-zero private GPU."""
    import httpx
    from scout.config import env

    cfg = config()["models"][entry]
    if not env(cfg.get("key_env", "")):
        return "missing key"
    if cfg.get("budget_guard"):
        return (
            "configured (GPU not started)"
            if env(cfg.get("base_url_env", ""))
            else "missing endpoint"
        )
    if cfg["model"].startswith("google:"):
        r = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + cfg["model"].split(":", 1)[1],
            headers={"x-goog-api-key": env(cfg["key_env"])},
            timeout=20,
        )
        return "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
    return "configured (provider has no free metadata check)"
