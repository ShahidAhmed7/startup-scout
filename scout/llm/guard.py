"""Every model request passes durable caps, concurrency, spacing and usage recording."""

import asyncio
import os
import time
from collections import defaultdict
from contextlib import contextmanager
from pydantic_ai.models.wrapper import WrapperModel
from scout.config import config
from scout.db import repo


class CapReached(RuntimeError):
    pass


_busy = defaultdict(int)
_slots = {}
_next = defaultdict(float)


@contextmanager
def usage_connection(conn):
    # A rollback-only dry run must still record quota and GPU usage durably.
    with repo.connect() as own:
        yield own


def reserve_attempt(conn, entry, job):
    """An advisory lock and running usage row prevent concurrent workers exceeding caps."""
    cfg = config()["models"][entry]
    provider, model = cfg["model"].split(":", 1)
    is_gpu = cfg.get("budget_guard", False)
    physical_key = cfg.get("key_env", entry)
    aliases = [
        name
        for name, value in config()["models"].items()
        if value["model"] == cfg["model"] and value.get("key_env", name) == physical_key
    ]
    # Include the old usage labels so rollout cannot accidentally reset today's quota.
    if physical_key == "GEMINI_API_KEY_MAIN":
        aliases.append("main")
    if physical_key == "GEMINI_API_KEY_BACKUP":
        aliases.append("backup")
    with conn.transaction():
        conn.execute(
            "select pg_advisory_xact_lock(hashtext(%s))",
            ("scout-gpu-budget" if is_gpu else model + ":" + physical_key,),
        )
        if is_gpu:
            from scout.budget import month_spend

            estimate = (
                cfg.get("cold_start_seconds", 480)
                + cfg.get("settings", {}).get("timeout_s", 1200)
                + 120
            )
            cost = estimate * config()["budget"]["a100_80gb_usd_per_second"]
            if month_spend(conn) + cost >= config()["budget"]["modal_stop_usd"]:
                raise CapReached("GPU budget reserved or exhausted")
        if "cap_per_day" in cfg:
            used = conn.execute(
                """select count(*) n from usage where model_id = %s and key_label = any(%s)
                and status not in ('quota','cap','network') and created_at >= %s""",
                (model, aliases, repo.pacific_day_start()),
            ).fetchone()["n"]
            cap = cfg["cap_per_day"] - (
                cfg.get("reserve_for_chat", 0)
                if job not in config()["chat_jobs"]
                else 0
            )
            if used >= cap:
                raise CapReached("Daily cap reached; remaining calls reserved for chat")
        return conn.execute(
            """insert into usage(provider,model_id,key_label,kind,status,gpu_seconds)
            values (%s,%s,%s,%s,'running',%s) returning id""",
            (
                "qwen" if is_gpu else ("gemini" if provider == "google" else provider),
                model,
                entry,
                job,
                estimate if is_gpu else None,
            ),
        ).fetchone()["id"]


class GuardedModel(WrapperModel):
    def __init__(self, entry, wrapped, job, conn=None):
        super().__init__(wrapped)
        self.entry, self.job, self.conn = entry, job, conn
        self.last_attempt = None

    async def request(self, messages, model_settings, model_request_parameters):
        entry = self.entry
        cfg = config()["models"][entry]
        if os.environ.get("FORCE_GEMINI_FAIL") == "1" and cfg["model"].startswith(
            "google:"
        ):
            raise CapReached("Forced Google failure")
        sem = _slots.setdefault(entry, asyncio.Semaphore(cfg.get("concurrency", 1)))
        async with sem:
            gap = 60 / cfg["rpm"] + 0.5 if cfg.get("rpm") else 0
            slot = max(time.monotonic(), _next[entry])
            _next[entry] = slot + gap
            await asyncio.sleep(max(0, slot - time.monotonic()))
            with usage_connection(self.conn) as conn:
                try:
                    attempt = reserve_attempt(conn, entry, self.job)
                except CapReached:
                    provider, model = cfg["model"].split(":", 1)
                    repo.record_usage(
                        conn,
                        provider="qwen"
                        if cfg.get("budget_guard")
                        else ("gemini" if provider == "google" else provider),
                        model_id=model,
                        key_label=entry,
                        kind=self.job,
                        status="cap",
                    )
                    raise
                self.last_attempt = attempt
                start = time.monotonic()
                response = None
                status = "server"
                try:
                    response = await self.wrapped.request(
                        messages, model_settings, model_request_parameters
                    )
                    status = "ok"
                    return response
                except Exception as exc:
                    code = getattr(exc, "status_code", None)
                    status = (
                        "quota"
                        if code == 429
                        else "auth"
                        if code in (401, 403)
                        else "server"
                    )
                    raise
                finally:
                    seconds = time.monotonic() - start
                    usage = response.usage if response else None
                    gpu = (
                        seconds + cfg.get("cold_start_seconds", 480) + 120
                        if cfg.get("budget_guard")
                        else None
                    )
                    conn.execute(
                        """update usage set status=%s,tokens_in=%s,tokens_out=%s,seconds=%s,gpu_seconds=%s
                        where id=%s""",
                        (
                            status,
                            usage.input_tokens if usage else None,
                            usage.output_tokens if usage else None,
                            seconds,
                            gpu,
                            attempt,
                        ),
                    )
