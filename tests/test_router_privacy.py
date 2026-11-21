from scout import cards


def test_fact_gate_drops_unsourced_names_and_numbers():
    material = "End Close raised EUR 3.2m from Frontline to fix payment mismatches."
    data = {
        "what_they_do": "End Close is a Y Combinator-backed startup. It fixes payment mismatches.",
        "evidence": "It raised EUR 3.2m from Frontline. It has 500 customers.",
        "bangladesh_play": "A suggested adaptation.",
    }
    fixed, removed = cards.fact_gate(
        data, material, "End Close", "Fix payment mismatches"
    )
    assert fixed["what_they_do"] == "It fixes payment mismatches."
    assert fixed["evidence"] == "It raised EUR 3.2m from Frontline."
    assert len(removed) == 2


def test_gate_keeps_legacy_card_readable():
    fixed, removed = cards.fact_gate(
        {"what_they_do": "Backed by Sequoia.", "why_funded": ""},
        "nothing",
        "X",
        "A tool",
    )
    assert fixed["what_they_do"] == "A tool" and fixed["why_funded"] == "unknown"


def test_private_server_protects_non_openai_inference_routes(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from scout.llm.private_server import BearerGate

    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    app = FastAPI()
    app.add_middleware(BearerGate)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/v1/chat/completions")
    def completion():
        return {"ok": True}

    @app.post("/invocations")
    def invocation():
        return {"secret": "private model output"}

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.post("/v1/chat/completions").status_code == 401
    assert (
        client.post(
            "/v1/chat/completions", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/chat/completions", headers={"Authorization": "Bearer test-key"}
        ).status_code
        == 200
    )
    assert client.post("/invocations").status_code == 401
    assert (
        client.post(
            "/invocations", headers={"Authorization": "Bearer test-key"}
        ).status_code
        == 404
    )
