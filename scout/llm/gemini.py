"""Gemini REST client.

Hard rule 4: always try GEMINI_API_KEY_MAIN first; switch to GEMINI_API_KEY_BACKUP only when
the main key fails (error, quota/limit, our own cap, revoked key). If both fail, raise
GeminiUnavailable so the router (phase 3) can fall back to Qwen on Modal.

Hard rule 5: only public data may be sent here. Private notes go to Qwen only.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from scout.config import config, env

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
KEYS = (("main", "GEMINI_API_KEY_MAIN"), ("backup", "GEMINI_API_KEY_BACKUP"))


class GeminiError(Exception):
    """kind: quota | auth | server | network | bad_request | bad_output | cap"""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status = status


class GeminiUnavailable(Exception):
    """Both keys failed. errors = [(key_label, GeminiError), ...]"""

    def __init__(self, errors: list[tuple[str, GeminiError]]):
        super().__init__("; ".join(f"{label}: {err}" for label, err in errors) or "no Gemini key configured")
        self.errors = errors


@dataclass
class Result:
    text: str
    data: object | None
    model_id: str
    key_label: str
    tokens_in: int = 0
    tokens_out: int = 0
    raw: dict = field(default_factory=dict, repr=False)


def classify(status: int, body: str) -> str:
    """Map an HTTP error to a kind. Everything except bad_request makes us try the backup key."""
    low = body.lower()
    if status == 429 or "resource_exhausted" in low or "quota" in low:
        return "quota"
    if status in (401, 403) or "api_key_invalid" in low or "api key not valid" in low or "permission_denied" in low:
        return "auth"
    if status >= 500:
        return "server"
    return "bad_request"


def model_id_for(alias: str) -> str:
    return config()["models"][f"gemini_{alias}"]


# ---------- per-minute spacing (stay under the free tier's requests/minute) ----------

_last_call: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def _throttle(key_label: str, alias: str) -> None:
    rpm = config()["gemini_limits"][alias]["rpm"]
    gap = 60.0 / rpm + 0.5
    with _lock:
        last = _last_call.get((key_label, alias), 0.0)
        wait = last + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[(key_label, alias)] = time.monotonic()


RETRY_SAME_KEY = {"bad_output", "server", "network"}
BACKOFF_SECONDS = 15


def _backoff() -> None:
    time.sleep(BACKOFF_SECONDS)


def _call_once(http: httpx.Client, key: str, model_id: str, prompt: str, *, schema: dict | None,
               system: str | None, temperature: float | None, thinking_level: str | None,
               max_output_tokens: int, timeout: float) -> dict:
    gen: dict = {"maxOutputTokens": max_output_tokens}
    if temperature is not None:
        gen["temperature"] = temperature
    if schema is not None:
        gen["responseMimeType"] = "application/json"
        gen["responseJsonSchema"] = schema
    if thinking_level:
        gen["thinkingConfig"] = {"thinkingLevel": thinking_level}
    body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    try:
        r = http.post(f"{BASE_URL}/models/{model_id}:generateContent",
                      headers={"x-goog-api-key": key}, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise GeminiError("network", type(e).__name__) from None
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")[:300]
        except ValueError:
            msg = r.text[:300]
        raise GeminiError(classify(r.status_code, r.text), msg, r.status_code)
    return r.json()


def _parse(raw: dict, model_id: str, key_label: str, want_json: bool) -> Result:
    cands = raw.get("candidates") or []
    if not cands:
        reason = (raw.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise GeminiError("bad_output", f"empty response ({reason})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if cands[0].get("finishReason") not in (None, "STOP"):
        raise GeminiError("bad_output", f"finishReason={cands[0].get('finishReason')}")
    data = None
    if want_json:
        try:
            data = json.loads(text)
        except ValueError:
            raise GeminiError("bad_output", "response was not valid JSON") from None
    meta = raw.get("usageMetadata") or {}
    return Result(text=text, data=data, model_id=model_id, key_label=key_label,
                  tokens_in=meta.get("promptTokenCount", 0),
                  tokens_out=meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0),
                  raw=raw)


def generate(alias: str, prompt: str, *, kind: str, schema: dict | None = None, system: str | None = None,
             validate: Callable[[object], None] | None = None, temperature: float | None = None,
             thinking_level: str | None = None, max_output_tokens: int | None = None,
             conn=None, only_key: str | None = None, http: httpx.Client | None = None) -> Result:
    """Call Gemini `alias` ("flash" or "flash_lite"): main key, then backup key.

    - schema: JSON schema for structured output; the reply is parsed into Result.data.
    - validate: extra check on Result.data; raise ValueError to reject. Bad output is retried once
      on the same key, then we move to the next key.
    - thinking_level / max_output_tokens / temperature: default to the `llm` section of config.yaml.
    - conn: database connection; if given, enforces our daily caps and records every attempt in `usage`.
    """
    from scout.db import repo  # local import keeps tests free of a database

    llm = config()["llm"]
    thinking_level = thinking_level or llm["thinking_level"]
    max_output_tokens = max_output_tokens or llm["max_output_tokens"]
    temperature = temperature if temperature is not None else llm.get("temperature")
    timeout = float(llm["request_timeout_seconds"])
    model_id = model_id_for(alias)
    cap = config()["caps"][f"gemini_{alias}_per_day"]
    own_http = http is None
    http = http or httpx.Client()
    errors: list[tuple[str, GeminiError]] = []

    def log(label: str, status: str, res: Result | None = None) -> None:
        if conn is not None:
            repo.record_usage(conn, provider="gemini", model_id=model_id, key_label=label, kind=kind,
                              status=status, tokens_in=res.tokens_in if res else None,
                              tokens_out=res.tokens_out if res else None)

    try:
        for label, env_name in KEYS:
            if only_key and label != only_key:
                continue
            key = env(env_name)
            if not key:
                continue
            if conn is not None and repo.gemini_calls_today(conn, model_id, label) >= cap:
                err = GeminiError("cap", f"our daily cap of {cap} reached")
                log(label, "cap")
                errors.append((label, err))
                continue
            for attempt in (1, 2):
                _throttle(label, alias)
                try:
                    raw = _call_once(http, key, model_id, prompt, schema=schema, system=system,
                                     temperature=temperature, thinking_level=thinking_level,
                                     max_output_tokens=max_output_tokens, timeout=timeout)
                    res = _parse(raw, model_id, label, want_json=schema is not None)
                    if validate is not None:
                        try:
                            validate(res.data)
                        except ValueError as e:
                            raise GeminiError("bad_output", f"failed validation: {e}") from None
                    log(label, "ok", res)
                    return res
                except GeminiError as e:
                    log(label, e.kind)
                    if e.kind == "bad_request":
                        raise  # our request is wrong; another key won't help
                    if e.kind in RETRY_SAME_KEY and attempt == 1:
                        if e.kind in ("server", "network"):
                            _backoff()  # "high demand" 503s and timeouts are usually short-lived
                        continue  # retry once on the same key
                    errors.append((label, e))
                    break  # move on to the next key
        raise GeminiUnavailable(errors)
    finally:
        if own_http:
            http.close()


def check_key(key_label: str, http: httpx.Client | None = None) -> dict[str, str]:
    """Free check (no quota used): can this key see our two models? Returns {alias: 'ok' | error}."""
    env_name = dict(KEYS)[key_label]
    key = env(env_name)
    if not key:
        return {"flash": "no key", "flash_lite": "no key"}
    out = {}
    with (http or httpx.Client()) as c:
        for alias in ("flash", "flash_lite"):
            r = c.get(f"{BASE_URL}/models/{model_id_for(alias)}", headers={"x-goog-api-key": key}, timeout=20)
            out[alias] = "ok" if r.status_code == 200 else f"HTTP {r.status_code} ({classify(r.status_code, r.text)})"
    return out
