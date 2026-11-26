"""The sole synchronous AI interface; one event loop owns cached provider clients."""

import asyncio
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
import json
import random
import threading
from dataclasses import dataclass
from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput
from scout.config import config
from scout.llm import registry, guard


class LLMUnavailable(RuntimeError):
    transient_only = False


@dataclass
class Result:
    output: object
    model: str
    entry: str
    usage: object = None

    @property
    def data(self):
        return (
            self.output.model_dump(by_alias=True)
            if isinstance(self.output, BaseModel)
            else self.output
        )

    @property
    def text(self):
        return (
            json.dumps(self.data, ensure_ascii=False)
            if not isinstance(self.output, str)
            else self.output
        )

    @property
    def model_id(self):
        return self.model.split(":", 1)[-1]

    @property
    def key_label(self):
        return self.entry


_loop = None
_loop_lock = threading.Lock()


def event_loop():
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(
                target=_loop.run_forever, daemon=True, name="scout-ai"
            ).start()
        return _loop


def chain_for(job, private):
    cfg = config()
    if private:
        return cfg["jobs"]["private"]
    jobs = cfg.get("dev_jobs", {}) if os.environ.get("SCOUT_DEV") == "1" else {}
    return jobs.get(job, cfg["jobs"].get(job, []))


# Google's free tier answers 503 "high demand" in bursts. Retry the same entry after these pauses (seconds),
# then the other key, then the next group; if the whole chain only met overloads, wait and try it once more.
BACKOFF = (4, 15)
CHAIN_PATIENCE = 45

# When Google's servers can't be reached at all (2026-11-26: every call from Modal failed to connect for 3.5
# hours), trying each batch on every key and model only burns the run's hour. After this many network failures
# in a row on Google entries, treat Google as unreachable for a while and fail its calls at once.
NET_TRIP = 8
NET_COOLDOWN_S = 600
_net = {"fails": 0, "until": 0.0, "trips": 0}


class GoogleUnreachable(LLMUnavailable):
    transient_only = True


def _is_google(entry) -> bool:
    return config()["models"][entry]["model"].startswith("google:")


def google_unreachable() -> bool:
    import time

    return time.monotonic() < _net["until"]


def google_trips() -> int:
    """How often Google was declared unreachable in this process (the pipeline reads it for its error)."""
    return _net["trips"]


def _net_result(entry, exc=None) -> None:
    import time

    if not _is_google(entry):
        return
    if exc is None:
        _net["fails"] = 0
    elif guard.network_failure(exc):
        _net["fails"] += 1
        if _net["fails"] >= NET_TRIP and not google_unreachable():
            _net["until"] = time.monotonic() + NET_COOLDOWN_S
            _net["trips"] += 1
            _net["fails"] = 0


async def _run(job, prompt, output_type, **kw):
    try:
        return await _run_chain(job, prompt, output_type, **kw)
    except LLMUnavailable as exc:
        if not exc.transient_only or google_unreachable():
            raise
    await asyncio.sleep(CHAIN_PATIENCE + random.uniform(0, 10))
    return await _run_chain(job, prompt, output_type, **kw)


async def _run_chain(
    job, prompt, output_type, *, system=None, private=False, conn=None, validate=None
):
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    chain = chain_for(job, private)
    errors = []
    transient_only = True
    for group in chain:
        left = list(group)
        while left:
            entry = min(left, key=lambda e: (guard._busy[e], group.index(e)))
            left.remove(entry)
            guard._busy[entry] += 1
            try:
                entry_cfg = config()["models"][entry]
                if private and (
                    not entry_cfg.get("budget_guard")
                    or not entry_cfg.get("base_url_env")
                    or entry_cfg["model"].startswith("google:")
                ):
                    raise LLMUnavailable("Private chain contains a non-private model")
                if os.environ.get("FORCE_GEMINI_FAIL") == "1" and entry_cfg[
                    "model"
                ].startswith("google:"):
                    raise guard.CapReached("Forced Google failure")
                if _is_google(entry) and google_unreachable():
                    raise GoogleUnreachable("Google was unreachable moments ago")
                model = guard.GuardedModel(entry, registry.get_model(entry), job, conn)
                retries = 0  # bad output and unknown errors: one retry on the same entry
                waits = list(BACKOFF)  # overloads and timeouts: a few patient retries
                while True:
                    try:
                        agent = Agent(
                            model=model,
                            output_type=NativeOutput(output_type)
                            if output_type
                            else str,
                            system_prompt=system or (),
                            model_settings=registry.settings(entry),
                            retries=0,
                        )
                        result = await agent.run(prompt)
                        _net_result(entry)
                        if validate:
                            validate(result.output)
                        return Result(
                            result.output,
                            entry_cfg["model"],
                            entry,
                            result.usage,
                        )
                    except guard.CapReached:
                        raise
                    except Exception as exc:
                        bad_output = isinstance(
                            exc, (ValueError, UnexpectedModelBehavior)
                        )
                        if model.last_attempt and bad_output:
                            with guard.usage_connection(conn) as log_conn:
                                log_conn.execute(
                                    "update usage set status='bad_output' where id=%s",
                                    (model.last_attempt,),
                                )
                        _net_result(entry, exc)
                        if google_unreachable() and _is_google(entry):
                            raise GoogleUnreachable("Google is unreachable") from exc
                        if not bad_output and guard.transient(exc) and waits:
                            await asyncio.sleep(waits.pop(0) + random.uniform(0, 2))
                            continue
                        if retries or getattr(exc, "status_code", None) in (
                            400,
                            401,
                            403,
                            404,
                            429,
                        ) or (not bad_output and guard.transient(exc)):
                            raise
                        retries += 1
                        await asyncio.sleep(1)
            except Exception as exc:
                # Never persist provider exception bodies (they can include prompts/credentials).
                errors.append(entry + ":" + type(exc).__name__)
                if not guard.transient(exc):
                    transient_only = False
            finally:
                guard._busy[entry] -= 1
    err = LLMUnavailable("No model succeeded: " + ", ".join(errors))
    err.transient_only = bool(errors) and transient_only
    raise err


def run(
    job,
    prompt,
    output_type=None,
    *,
    system=None,
    private=False,
    conn=None,
    validate=None,
):
    return asyncio.run_coroutine_threadsafe(
        _run(
            job,
            prompt,
            output_type,
            system=system,
            private=private,
            conn=conn,
            validate=validate,
        ),
        event_loop(),
    ).result()


def run_many(job, prompts, output_type=None, **kw):
    async def all_():
        return await asyncio.gather(
            *[_run(job, p, output_type, **kw) for p in prompts], return_exceptions=True
        )

    return asyncio.run_coroutine_threadsafe(all_(), event_loop()).result()
