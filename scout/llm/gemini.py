"""Gemini API client (Gemini 4 Hydrogen, Flash-Lite, Gemma 4 — all on the free tier).

Keys (owner, 2026-11-19, updates hard rule 4): both keys work IN PARALLEL. Each call claims a
free key (main if both are free), so while one key is busy the other takes the next task. If the
claimed key fails (error, quota/limit, our own cap, revoked key), the call fails over to the other
key. run_parallel() runs several jobs at once across both keys.
Owner's fallback order: if a model fails on both keys, generate_chain() tries the next free model
from config.yaml `fallback`; only when all fail does the router (phase 3) use Qwen on Modal.

Hard rule 5: only public data may be sent here. Private notes go to Qwen only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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


GOOGLE_ALIASES = ("hydrogen", "flash_lite", "gemma")  # models served by the Gemini API
CHAT_KINDS = {"deep_dive", "ask", "kit", "test"}  # owner-initiated: may use the reserved Hydrogen calls


def model_cfg(alias: str) -> dict:
    return config()["models"][alias]


def model_id_for(alias: str) -> str:
    return model_cfg(alias)["id"]


# ---------- per-minute spacing (stay under the free tier's requests/minute) ----------

_next_slot: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def _throttle(key_label: str, alias: str) -> None:
    """Space calls per (key, model) to respect requests/minute. Each key has its own clock, so a
    wait on one key never holds up the other key."""
    rpm = model_cfg(alias)["free_limits"]["rpm"]
    gap = 60.0 / rpm + 0.5
    with _lock:  # only book the slot under the lock; sleep outside it
        slot = max(time.monotonic(), _next_slot.get((key_label, alias), 0.0))
        _next_slot[(key_label, alias)] = slot + gap
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


# ---------- key pool: both keys work in parallel ----------

_busy: dict[tuple[str, str], int] = {}
_pool = threading.Condition()


def _slots(alias: str) -> int:
    return int(model_cfg(alias).get("concurrency_per_key", 1))


@contextmanager
def _claim_key(alias: str, labels: list[str]):
    """Claim a key for this model: the one with the fewest requests in flight (main first on ties).
    Each key runs up to `concurrency_per_key` requests at once; waits if every key is full."""
    slots = _slots(alias)
    with _pool:
        while all(_busy.get((alias, l), 0) >= slots for l in labels):
            _pool.wait()
        label = min((l for l in labels if _busy.get((alias, l), 0) < slots),
                    key=lambda l: (_busy.get((alias, l), 0), labels.index(l)))
        _busy[(alias, label)] = _busy.get((alias, label), 0) + 1
    try:
        yield label
    finally:
        with _pool:
            _busy[(alias, label)] -= 1
            _pool.notify_all()


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
    """Call one model (`alias` from config.yaml models: "hydrogen", "flash_lite", "gemma"): main key, then backup key.

    - schema: JSON schema for structured output; the reply is parsed into Result.data.
    - validate: extra check on Result.data; raise ValueError to reject. Bad output is retried once
      on the same key, then we move to the next key.
    - thinking_level / max_output_tokens / temperature: default to the `llm` section of config.yaml.
    - conn: database connection; if given, enforces our daily caps and records every attempt in `usage`.
    """
    from scout.db import repo  # local import keeps tests free of a database

    llm = config()["llm"]
    mcfg = model_cfg(alias)
    thinking_level = thinking_level or llm["thinking_level"]
    max_output_tokens = min(max_output_tokens or llm["max_output_tokens"], mcfg["max_output_tokens"])
    temperature = temperature if temperature is not None else llm.get("temperature")
    timeout = float(llm["request_timeout_seconds"])
    model_id = mcfg["id"]
    cap = mcfg["cap_per_day"]
    own_http = http is None
    http = http or httpx.Client()
    errors: list[tuple[str, GeminiError]] = []

    def log(label: str, status: str, res: Result | None = None) -> None:
        if conn is not None:
            repo.record_usage(conn, provider="gemini", model_id=model_id, key_label=label, kind=kind,
                              status=status, tokens_in=res.tokens_in if res else None,
                              tokens_out=res.tokens_out if res else None)

    try:
        # Pipeline jobs leave `reserve_for_chat` calls of this model for the owner's deep dives and asks.
        reserve = mcfg.get("reserve_for_chat", 0)
        if conn is not None and reserve and kind not in CHAT_KINDS and only_key is None:
            used = sum(repo.gemini_calls_today(conn, model_id, label) for label, _ in KEYS)
            if used >= len(KEYS) * cap - reserve:
                for label, _ in KEYS:
                    log(label, "cap")
                raise GeminiUnavailable([(label, GeminiError("cap", f"last {reserve} calls kept for chat"))
                                         for label, _ in KEYS])
        labels = [label for label, env_name in KEYS
                  if env(env_name) and (only_key is None or label == only_key)]
        if not labels:
            raise GeminiUnavailable([])
        # Claim a free key (both keys work in parallel); fail over to the other key if it fails.
        with _claim_key(alias, labels) as first:
            for label in [first, *[l for l in labels if l != first]]:
                key = env(dict(KEYS)[label])
                if conn is not None and repo.gemini_calls_today(conn, model_id, label) >= cap:
                    log(label, "cap")
                    errors.append((label, GeminiError("cap", f"our daily cap of {cap} reached")))
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
                        break  # fail over to the other key
        raise GeminiUnavailable(errors)
    finally:
        if own_http:
            http.close()


def generate_chain(alias: str, prompt: str, **kwargs) -> Result:
    """Try `alias`, then each model in config.yaml `fallback[alias]` (all free).

    Raises GeminiUnavailable (with every error) when all of them fail; the router then uses
    Qwen on Modal (phase 3). The model that answered is in Result.model_id: store it on the card.
    """
    errors: list[tuple[str, GeminiError]] = []
    for a in [alias, *config()["fallback"].get(alias, [])]:
        try:
            return generate(a, prompt, **kwargs)
        except GeminiUnavailable as e:
            errors += [(f"{a}/{label}", err) for label, err in e.errors]
    raise GeminiUnavailable(errors)


def run_job(job: str, prompt: str, **kwargs) -> Result:
    """Run a pipeline/chat job ("extract", "score", "card", "deep_dive", "friday", "ask") on the model
    config.yaml `jobs` assigns it (Hydrogen first wherever it fits), with the fallback chain behind it."""
    alias = config()["jobs"][job]
    if os.environ.get("SCOUT_DEV") == "1" and alias == "hydrogen":
        alias = "flash_lite"  # dev runs save Hydrogen's 20/day for the real run
    return generate_chain(alias, prompt, kind=job, **kwargs)


def run_parallel(tasks: list[tuple[str, str, dict]], workers: int | None = None) -> list[Result | Exception]:
    """Run several jobs at once, spread across both keys (owner, 2026-11-19).

    tasks: [(job, prompt, kwargs), ...]. Returns one entry per task, in order: the Result, or the
    exception if that task failed (so one failure never loses the others' work).
    Example: 5 cards -> main and backup each write one at a time, side by side.
    """
    if workers is None:
        aliases = {config()["jobs"][job] for job, _, _ in tasks} or {"flash_lite"}
        workers = len(KEYS) * max(_slots(a) for a in aliases)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(run_job, job, prompt, **kw) for job, prompt, kw in tasks]
        out: list[Result | Exception] = []
        for f in futures:
            err = f.exception()
            out.append(err if err is not None else f.result())
        return out


def check_key(key_label: str, http: httpx.Client | None = None) -> dict[str, str]:
    """Free check (no quota used): can this key see our models? Returns {alias: 'ok' | error}."""
    env_name = dict(KEYS)[key_label]
    key = env(env_name)
    if not key:
        return {a: "no key" for a in GOOGLE_ALIASES}
    out = {}
    with (http or httpx.Client()) as c:
        for alias in GOOGLE_ALIASES:
            r = c.get(f"{BASE_URL}/models/{model_id_for(alias)}", headers={"x-goog-api-key": key}, timeout=20)
            out[alias] = "ok" if r.status_code == 200 else f"HTTP {r.status_code} ({classify(r.status_code, r.text)})"
    return out
