"""2026-11-26: Google could not be reached from Modal for 3.5 hours. The run must fail fast, keep retrying through
the morning, tell the owner the digest is late, and never send the bulk jobs to the paid GPU."""

import asyncio
from datetime import datetime, timedelta

import pytest

from scout import scheduler
from scout.config import config
from scout.llm import guard, registry, run as ai

DHAKA = scheduler.DHAKA


def at(hh, mm=0):
    return datetime(2026, 11, 26, hh, mm, tzinfo=DHAKA)


class Conn:
    """Just enough of a connection for scheduler.tick."""

    def __init__(self):
        self.sql = []

    def execute(self, sql, args=None):
        self.sql.append(sql)
        return self

    def fetchone(self):
        return None  # no digest yet

    def fetchall(self):
        return []  # no chat settings


def run(started, error=None, status="error", minutes=5):
    return {"status": status, "started_at": started, "finished_at": started + timedelta(minutes=minutes),
            "error": error}


@pytest.fixture
def sched(monkeypatch):
    state = {"pipeline": [], "late_note": [], "sent": [], "spawned": 0}
    monkeypatch.setattr(scheduler, "_runs_today", lambda conn, kind, day: state.get(kind, []))
    monkeypatch.setattr(scheduler.outbox, "deliver", lambda conn, payloads, kind: state["sent"].append(
        (kind, payloads[0]["text"]["body"] if "text" in payloads[0] else payloads)))
    monkeypatch.setattr(scheduler.outbox, "text", lambda body: [{"text": {"body": body}}])

    from scout.db import repo

    def start_run(conn, kind):
        state.setdefault(kind, []).append(run(datetime.now(DHAKA), status="ok"))
        return 1

    monkeypatch.setattr(repo, "start_run", start_run)
    monkeypatch.setattr(repo, "finish_run", lambda *a, **k: None)
    from scout import budget

    monkeypatch.setattr(budget, "warning_due", lambda conn: None)

    def tick(now):
        def spawn():
            state["spawned"] += 1

        return scheduler.tick(Conn(), now=now, spawn={"pipeline": spawn})

    state["tick"] = tick
    return state


def test_google_down_retries_every_30_minutes_until_13(sched):
    sched["pipeline"] = [run(at(7, 0), "GoogleUnreachable")]  # failed fast at 07:05
    assert sched["tick"](at(7, 15)) == []  # waiting: only 10 minutes since it ended
    assert "pipeline started" in sched["tick"](at(7, 45))
    sched["pipeline"].insert(0, run(at(12, 30), "GoogleUnreachable"))
    assert sched["tick"](at(13, 0)) == ["skipped note"]  # past 13:00: give up for today
    assert "couldn't be reached" in sched["sent"][-1][1]


def test_late_note_once_at_digest_time_while_google_is_down(sched):
    sched["pipeline"] = [run(at(7, 0), "GoogleUnreachable")]
    assert sched["tick"](at(8, 0)) == ["pipeline started", "late note"]
    assert "late today" in sched["sent"][-1][1] and "13:00" in sched["sent"][-1][1]
    assert "late note" not in sched["tick"](at(8, 15))  # once a day
    assert len([s for s in sched["sent"] if s[0] == "late_note"]) == 1


def test_attempt_cap_ends_the_day(sched):
    sched["pipeline"] = [run(at(7, 0) + timedelta(minutes=i), "GoogleUnreachable") for i in range(10)]
    assert sched["tick"](at(9, 0)) == ["skipped note"]


def test_other_failures_keep_the_single_retry(sched):
    sched["pipeline"] = [run(at(7, 0), "RuntimeError")]
    assert "pipeline started" in sched["tick"](at(7, 15))
    sched["pipeline"].insert(0, run(at(7, 15), "RuntimeError"))
    acts = sched["tick"](at(8, 0))
    assert acts == ["skipped note"]
    assert sched["sent"][-1][1].startswith("Skipped today: the AI models were unavailable")


def test_a_run_modal_stopped_counts_as_failed_after_70_minutes(sched):
    sched["pipeline"] = [run(at(7, 0), status="running")]
    assert sched["tick"](at(8, 0)) == []  # still running (60 minutes)
    conn = Conn()
    scheduler._close_stuck_runs(conn, at(8, 15))
    assert any("update runs set status = 'error'" in s for s in conn.sql)


def test_bulk_jobs_never_fall_back_to_the_gpu_but_cards_and_chat_do():
    jobs = config()["jobs"]
    for job in ("extract", "classify", "score"):
        assert all("private" not in group for group in jobs[job]), job
    for job in ("card", "ask", "deep_dive"):
        assert jobs[job][-1] == ["private"], job


def test_budget_stop_is_30():
    assert config()["budget"]["modal_stop_usd"] == 30


def test_google_client_waits_30_seconds_to_connect():
    client = registry._google_http_client(300)
    assert client.timeout.connect == 30 and client.timeout.read == 300


def _conn_error(phase):
    from pydantic_ai.exceptions import ModelTimeoutError

    return ModelTimeoutError(model_name="gemini-3.5-flash-lite", message="timed out", phase=phase)


def test_error_kind_names_the_failed_stage_and_network_failures():
    exc = _conn_error("connect")
    assert guard.error_kind(exc) == "ModelTimeoutError/connect"
    assert guard.network_failure(exc)
    assert not guard.network_failure(_conn_error("read"))  # a slow answer is not an unreachable server
    assert not guard.network_failure(ValueError("bad json"))


@pytest.fixture
def breaker(monkeypatch):
    ai._net.update(fails=0, until=0.0, trips=0)
    yield
    ai._net.update(fails=0, until=0.0, trips=0)


def test_breaker_trips_after_repeated_network_failures_and_skips_google(monkeypatch, breaker):
    calls = []

    class Model:
        last_attempt = None

        def __init__(self, entry, *a):
            self.entry = entry

    monkeypatch.setattr(ai.guard, "GuardedModel", Model)
    monkeypatch.setattr(ai.registry, "get_model", lambda entry: None)
    monkeypatch.setattr(ai.registry, "settings", lambda entry: {})
    monkeypatch.setattr(ai, "BACKOFF", (0, 0))

    class Agent:
        def __init__(self, model, **kw):
            self.model = model

        async def run(self, prompt):
            calls.append(self.model.entry)
            raise _conn_error("connect")

    monkeypatch.setattr(ai, "Agent", Agent)

    async def sleep(_):
        return None

    monkeypatch.setattr(ai.asyncio, "sleep", sleep)
    with pytest.raises(ai.LLMUnavailable):
        asyncio.run(ai._run("extract", "p", None))
    assert ai.google_unreachable() and ai.google_trips() == 1
    assert len(calls) == ai.NET_TRIP  # stopped as soon as it tripped, no 45 s second pass
    assert "private" not in calls
    calls.clear()
    with pytest.raises(ai.LLMUnavailable):
        asyncio.run(ai._run("extract", "p", None))
    assert calls == []  # while tripped, Google is not called at all


def test_a_success_resets_the_failure_count(breaker):
    for _ in range(ai.NET_TRIP - 1):
        ai._net_result("fast_main", _conn_error("connect"))
    ai._net_result("fast_backup")
    ai._net_result("fast_main", _conn_error("connect"))
    assert not ai.google_unreachable()
