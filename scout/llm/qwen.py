"""Qwen on Modal: the private model (hard rule 5) and the last-resort backup (CLAUDE.md section 5).

Batch mode: one call starts an A100 80GB container (weights cached in a Modal Volume), runs every prompt,
and the container shuts down shortly after. The budget guard is checked before every GPU call.
"""
from __future__ import annotations

import json
import time

from scout import budget
from scout.config import config
from scout.db import repo
from scout.llm.gemini import Result

APP_NAME = "startup-scout"


class QwenUnavailable(Exception):
    pass


def _extract_json(text: str):
    text = (text or "").strip()
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def complete_batch(prompts: list[str], *, job: str, schema: dict | None, system: str | None, validate=None,
                   conn=None) -> list[Result | Exception]:
    """Run prompts on Qwen. Returns a Result or an Exception per prompt."""
    if conn is not None and budget.gpu_blocked(conn):
        return [QwenUnavailable("Modal budget limit reached: GPU paused until the 1st") for _ in prompts]
    import modal

    model_id = config()["models"]["qwen"]["id"]
    requests = [{"system": system or "", "prompt": p, "schema": schema} for p in prompts]
    t0 = time.time()
    try:
        Qwen = modal.Cls.from_name(APP_NAME, "Qwen")
        outs = Qwen().generate.remote(requests)
    except Exception as e:
        if conn is not None:
            repo.record_usage(conn, provider="qwen", model_id=model_id, key_label="modal", kind=job,
                              status="server", gpu_seconds=round(time.time() - t0, 1))
        return [QwenUnavailable(f"Qwen call failed: {type(e).__name__}: {str(e)[:200]}") for _ in prompts]
    gpu_seconds = round(time.time() - t0, 1)  # upper bound: includes start-up; good enough for the budget
    if conn is not None:
        repo.record_usage(conn, provider="qwen", model_id=model_id, key_label="modal", kind=job, status="ok",
                          gpu_seconds=gpu_seconds, tokens_in=sum(o.get("tokens_in", 0) for o in outs),
                          tokens_out=sum(o.get("tokens_out", 0) for o in outs))
    results: list[Result | Exception] = []
    for o in outs:
        if o.get("error"):
            results.append(QwenUnavailable(o["error"]))
            continue
        try:
            data = _extract_json(o["text"]) if schema is not None else None
            if validate is not None and data is not None:
                validate(data)
            results.append(Result(text=o["text"], data=data, model_id=model_id, key_label="modal",
                                  tokens_in=o.get("tokens_in", 0), tokens_out=o.get("tokens_out", 0)))
        except ValueError as e:
            results.append(QwenUnavailable(f"bad output: {e}"))
    return results
