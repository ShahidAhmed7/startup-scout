"""Live SQL and message rendering checks; all mutations roll back and no messages leave the process."""

import json
import os
from contextlib import nullcontext
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import Rollback
from scout import archive, cards
from scout.config import require_env
from scout.db import repo
from scout.whatsapp import client, commands


def integration():
    root = Path(__file__).resolve().parents[1]
    saved = json.loads((root / "data/redesign/live-results.json").read_text())
    sample = next(c for c in saved["evidence"]["cards"] if c["kind"] == "card")
    sent = []
    original = client.send_payload
    client.send_payload = lambda payload: (
        sent.append(payload) or "dry-integration-" + str(len(sent))
    )
    try:
        with repo.connect() as conn:
            before = {
                table: conn.execute("select count(*) n from " + table).fetchone()["n"]
                for table in ("cards", "digests", "ideas", "signals", "scores")
            }
            queue_before = conn.execute(
                "select pending_queue_json from owner_state where id=1"
            ).fetchone()["pending_queue_json"]
            with conn.transaction():
                cid = conn.execute(
                    """insert into cards(startup_id,kind,body,source_urls,model_id,run_date,market,title,data_json)
                    values(%s,'card',%s,%s,%s,%s,%s,%s,%s) returning id""",
                    tuple(
                        sample[k]
                        for k in (
                            "startup_id",
                            "body",
                            "source_urls",
                            "model_id",
                            "run_date",
                            "market",
                            "title",
                        )
                    )
                    + (json.dumps(sample["data_json"]),),
                ).fetchone()["id"]
                app = FastAPI()
                archive.add_routes(app, lambda: nullcontext(conn))
                web = TestClient(app)
                auth = ("owner", require_env("ARCHIVE_PASSWORD"))
                assert web.get("/archive", follow_redirects=False).status_code == 303
                for params in (
                    {},
                    {"market": "global"},
                    {"min": "6.5"},
                    {"ambition": "lean"},
                    {"ambition": "venture"},
                    {"business_model": "commerce"},
                ):
                    response = web.get("/archive", params=params, auth=auth)
                    assert response.status_code == 200, (params, response.status_code)
                page = web.get("/archive/card/" + str(cid), auth=auth)
                assert (
                    page.status_code == 200
                    and "Why these scores" in page.text
                    and sample["model_id"] in page.text
                )
                (root / "data/redesign/archive-preview.html").write_text(page.text)
                commands.open_card(conn, cid)
                payload = sent[-1]["interactive"]
                assert (
                    payload["type"] == "button" and len(payload["body"]["text"]) <= 1024
                )
                assert [b["reply"]["title"] for b in payload["action"]["buttons"]] == [
                    "Save",
                    "Skip",
                    "More",
                ]
                commands.save_card(conn, cid)
                commands.skip_card(conn, cid)
                commands.handle(
                    conn, {"type": "text", "text": {"body": "ambition seed"}}
                )
                assert (
                    conn.execute(
                        "select value from settings where key='ambition'"
                    ).fetchone()["value"]
                    == "seed"
                )
                commands.handle(conn, {"type": "text", "text": {"body": "saved"}})
                print(
                    "Live archive SQL/auth/filters, full card, attached actions, Save/Skip, ambition and notebook: PASS"
                )
                raise Rollback()
            after = {
                table: conn.execute("select count(*) n from " + table).fetchone()["n"]
                for table in before
            }
            queue_after = conn.execute(
                "select pending_queue_json from owner_state where id=1"
            ).fetchone()["pending_queue_json"]
            assert before == after and queue_before == queue_after
            print("Rollback preserved production row counts and pending queue:", before)
    finally:
        client.send_payload = original
