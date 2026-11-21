"""Taste learning (phase 5): embeddings (pgvector) of saved vs skipped startups nudge ranking.

Never more than ±1 point, only after at least 3 Save/Skip signals, and the monthly report explains it.
Embeddings are made from public card/startup text only (gemini-embedding-2, free tier), main key first.
"""
from __future__ import annotations

import json
import math

import httpx

from scout.config import env, config
from scout.db import repo

EMBED_MODEL = config()["embedding_model"]
DIMS = 768
MIN_SIGNALS = 3
STRENGTH = 4.0  # similarity difference -> points (then clamped to ±1)


def _record(label: str, status: str) -> None:
    # Own connection, like every model call: a rolled-back dry run must still record its quota use.
    with repo.connect() as own:
        repo.record_usage(own, provider="gemini", model_id=EMBED_MODEL, key_label=label, kind="embed",
                          status=status)


def embed_texts(texts: list[str], conn=None) -> list[list[float]] | None:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:batchEmbedContents"
    for label, env_name in (("main", "GEMINI_API_KEY_MAIN"), ("backup", "GEMINI_API_KEY_BACKUP")):
        key = env(env_name)
        if not key:
            continue
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), 100):
                body = {"requests": [{"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": t[:2000]}]},
                                      "outputDimensionality": DIMS} for t in texts[i:i + 100]]}
                r = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=60)
                r.raise_for_status()
                out += [e["values"] for e in r.json()["embeddings"]]
            if conn is not None:
                _record(label, "ok")
            return out
        except Exception:
            if conn is not None:
                _record(label, "server")
    return None


def _text(r: dict) -> str:
    sj = r.get("scores_json") or {}
    return " · ".join(x for x in [r["name"], r.get("category") or "", sj.get("search_phrase", ""),
                                   r.get("description") or ""] if x)


def ensure_embeddings(conn, startup_ids: list[int]) -> int:
    rows = conn.execute(
        """select s.id, s.name, s.category, s.description, sc.scores_json from startups s
           left join lateral (select scores_json from scores where startup_id = s.id order by created_at desc limit 1) sc on true
           where s.id = any(%s) and s.embedding is null""", (startup_ids,)).fetchall()
    if not rows:
        return 0
    vecs = embed_texts([_text(r) for r in rows], conn)
    if not vecs:
        return 0
    for r, v in zip(rows, vecs):
        conn.execute("update startups set embedding = %s::extensions.vector where id = %s", (json.dumps(v), r["id"]))
    return len(rows)


def _vec(v) -> list[float]:
    return json.loads(v) if isinstance(v, str) else list(v)


def _mean(vs: list[list[float]]) -> list[float]:
    return [sum(x) / len(vs) for x in zip(*vs)]


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def profile(conn) -> tuple[list[list[float]], list[list[float]]]:
    rows = conn.execute(
        """select distinct on (c.startup_id, g.kind) g.kind, s.id, s.embedding::text as emb
           from signals g join cards c on c.id = g.card_id join startups s on s.id = c.startup_id
           where g.kind in ('save', 'skip')""").fetchall()
    ensure_embeddings(conn, [r["id"] for r in rows if r["emb"] is None])
    rows = conn.execute(
        """select distinct on (c.startup_id, g.kind) g.kind, s.embedding::text as emb
           from signals g join cards c on c.id = g.card_id join startups s on s.id = c.startup_id
           where g.kind in ('save', 'skip') and s.embedding is not null""").fetchall()
    saved = [_vec(r["emb"]) for r in rows if r["kind"] == "save"]
    skipped = [_vec(r["emb"]) for r in rows if r["kind"] == "skip"]
    return saved, skipped


def nudge(vec: list[float], saved: list[list[float]], skipped: list[list[float]]) -> float:
    if len(saved) + len(skipped) < MIN_SIGNALS:
        return 0.0
    s = _cos(vec, _mean(saved)) if saved else 0.0
    k = _cos(vec, _mean(skipped)) if skipped else 0.0
    return round(max(-1.0, min(1.0, (s - k) * STRENGTH)), 2)


def apply(conn, run_date) -> dict:
    """Embed today's scored startups and add the taste nudge to their ranks (scores.taste records it)."""
    saved, skipped = profile(conn)
    rows = conn.execute("select id, startup_id from scores where run_date = %s and not skip and taste is null",
                        (run_date,)).fetchall()
    if not rows or len(saved) + len(skipped) < MIN_SIGNALS:
        return {"applied": 0, "signals": len(saved) + len(skipped)}
    ensure_embeddings(conn, [r["startup_id"] for r in rows])
    embs = {r["id"]: _vec(r["emb"]) for r in conn.execute(
        "select id, embedding::text as emb from startups where id = any(%s) and embedding is not null",
        ([r["startup_id"] for r in rows],)).fetchall()}
    n = 0
    for r in rows:
        if r["startup_id"] not in embs:
            continue
        t = nudge(embs[r["startup_id"]], saved, skipped)
        conn.execute("update scores set taste = %s, rank_bd = rank_bd + %s, rank_global = rank_global + %s "
                     "where id = %s", (t, t, t, r["id"]))
        n += 1
    return {"applied": n, "signals": len(saved) + len(skipped)}
