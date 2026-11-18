"""Effective settings = config.yaml, overridden by values the owner set by chat (`settings` table)."""
from __future__ import annotations

import copy
import json

from scout.config import config

CHAT_KEYS = ("digest_time", "digest_mix", "skip_list", "owner_skills")


def effective(conn=None) -> dict:
    cfg = copy.deepcopy(config())
    if conn is None:
        return cfg
    for row in conn.execute("select key, value from settings").fetchall():
        v = row["value"]
        if row["key"] == "owner_skills":
            cfg["owner"]["skills"] = v
        elif row["key"] in cfg:
            cfg[row["key"]] = v
    return cfg


def set_value(conn, key: str, value) -> None:
    assert key in CHAT_KEYS
    conn.execute("""insert into settings (key, value) values (%s, %s)
                    on conflict (key) do update set value = excluded.value, updated_at = now()""",
                 (key, json.dumps(value)))
