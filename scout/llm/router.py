"""One interface for every LLM call (CLAUDE.md section 5):

    complete(job, prompt, schema=..., private=False)
    complete_many(job, prompts, schema=...)      # parallel across both Gemini keys

- private=True  → Qwen on Modal only (hard rule 5: private notes never go to Google).
- otherwise     → the model config.yaml `jobs` assigns (Hydrogen first), fallback chain
                  Flash-Lite → Gemma 4, and only then Qwen on Modal (if the budget guard allows).
- FORCE_GEMINI_FAIL=1 skips Google entirely (to test the Qwen fallback).
"""
from __future__ import annotations

import os

from scout.llm import gemini
from scout.llm.gemini import GeminiError, GeminiUnavailable, Result


class LLMUnavailable(Exception):
    pass


def _forced() -> bool:
    return os.environ.get("FORCE_GEMINI_FAIL") == "1"


def _qwen(prompts: list[str], *, job: str, schema, system, validate, conn) -> list[Result | Exception]:
    try:
        from scout.llm import qwen
    except ImportError:
        return [LLMUnavailable("Qwen backup not available") for _ in prompts]
    return qwen.complete_batch(prompts, job=job, schema=schema, system=system, validate=validate, conn=conn)


def complete(job: str, prompt: str, *, schema: dict | None = None, system: str | None = None,
             validate=None, private: bool = False, conn=None, **kw) -> Result:
    return complete_many(job, [prompt], schema=schema, system=system, validate=validate,
                         private=private, conn=conn, raise_errors=True, **kw)[0]


def complete_many(job: str, prompts: list[str], *, schema: dict | None = None, system: str | None = None,
                  validate=None, private: bool = False, conn=None, raise_errors: bool = False,
                  **kw) -> list[Result | Exception]:
    """Run many prompts for one job. Returns a Result or an Exception per prompt, in order."""
    if private:
        results = _qwen(prompts, job=job, schema=schema, system=system, validate=validate, conn=conn)
    else:
        if _forced():
            results: list = [GeminiUnavailable([("forced", GeminiError("server", "FORCE_GEMINI_FAIL=1"))])
                             for _ in prompts]
        else:
            tasks = [(job, p, dict(schema=schema, system=system, validate=validate, conn=conn, **kw))
                     for p in prompts]
            results = gemini.run_parallel(tasks)
        failed = [i for i, r in enumerate(results) if isinstance(r, GeminiUnavailable)]
        if failed:
            backup = _qwen([prompts[i] for i in failed], job=job, schema=schema, system=system,
                           validate=validate, conn=conn)
            for i, r in zip(failed, backup):
                results[i] = r
    if raise_errors:
        for r in results:
            if isinstance(r, Exception):
                raise r
    return results
