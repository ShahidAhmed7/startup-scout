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


# Statuses that never reached the model (rejected or blocked): they do not count against our daily caps.
NOT_COUNTED = ("quota", "cap", "network", "unavailable")


def transient(exc) -> bool:
    """Provider overload or a dropped connection: worth retrying later on the same model."""
    from pydantic_ai.exceptions import ModelUnavailableError

    code = getattr(exc, "status_code", None)
    names = type(exc).__name__ + " " + type(exc.__cause__ or exc).__name__
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

    return (
        isinstance(exc, ModelUnavailableError)
        or code in (500, 502, 503, 504)
        # a dropped connection (no HTTP status), e.g. a proxy closing a silent request
        or (isinstance(exc, ModelAPIError) and not isinstance(exc, ModelHTTPError) and code is None)
        or any(n in names for n in ("Timeout", "ConnectError", "RemoteProtocolError", "ReadError"))
    )


async def wait_until_ready(base_url: str, deadline_s: float) -> None:
    """Wake a scale-to-zero server and wait for its unauthenticated /health check (no data is sent).
    Modal holds each request up to 150 s while the container starts, so keep asking until the deadline."""
    import httpx

    if not base_url:
        raise RuntimeError("Private endpoint is not configured")
    url = base_url.rstrip("/").removesuffix("/v1") + "/health"
    end = time.monotonic() + deadline_s
    async with httpx.AsyncClient(timeout=160) as client:
        while True:
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > end:
                raise TimeoutError("Private model did not start in time")
            await asyncio.sleep(5)


def attempt_status(exc) -> str:
    from pydantic_ai.exceptions import ModelUnavailableError

    code = getattr(exc, "status_code", None)
    if code == 429:
        return "quota"
    if code in (401, 403):
        return "auth"
    # 503 "high demand": Google rejected the request before running the model.
    if isinstance(exc, ModelUnavailableError) or code == 503:
        return "unavailable"
    if "Timeout" in type(exc).__name__ + type(exc.__cause__ or exc).__name__:
        return "timeout"
    return "server"


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
                and status <> all(%s) and created_at >= %s""",
                (model, aliases, list(NOT_COUNTED), repo.pacific_day_start()),
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
                    if cfg.get("budget_guard") and cfg.get("base_url_env"):
                        from scout.config import env

                        await wait_until_ready(
                            env(cfg["base_url_env"]) or "",
                            2 * cfg.get("cold_start_seconds", 480),
                        )
                    if cfg.get("stream"):
                        # Modal closes a silent HTTP request after 150 s; streamed tokens keep long private
                        # requests alive. The collected response is identical to a non-streamed one.
                        async with self.wrapped.request_stream(
                            messages, model_settings, model_request_parameters
                        ) as stream:
                            async for _ in stream:
                                pass
                        response = stream.get()
                    else:
                        response = await self.wrapped.request(
                            messages, model_settings, model_request_parameters
                        )
                    status = "ok"
                    return response
                except Exception as exc:
                    status = attempt_status(exc)
                    raise
                finally:
                    seconds = time.monotonic() - start
                    usage = response.usage if response else None
                    gpu = None
                    if cfg.get("budget_guard"):
                        # One container serves everything: only the first request of a burst pays the cold
                        # start and the idle tail; later ones in the same window run on the warm GPU.
                        warm = conn.execute(
                            """select 1 from usage where provider='qwen' and id < %s and status <> 'cap'
                            and created_at > now() - make_interval(secs => %s) limit 1""",
                            (attempt, seconds + 300),
                        ).fetchone()
                        # The measured time already includes any cold start (the readiness wait runs inside
                        # it); the first request of a burst also pays the idle tail before scale-down.
                        gpu = seconds + (0 if warm else cfg.get("scaledown_seconds", 120))
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
