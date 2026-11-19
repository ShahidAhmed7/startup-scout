"""Hard rule 5: private data never goes to Gemini. Fallback order: Gemini chain first, Qwen last."""
import pytest

from scout.llm import gemini, router
from scout.llm.gemini import GeminiError, GeminiUnavailable, Result


@pytest.fixture
def fake_qwen(monkeypatch):
    calls = []

    def complete_batch(prompts, **kw):
        calls.append(prompts)
        return [Result(text="{}", data={"ok": True}, model_id="Qwen/Qwen3.6-27B-FP8", key_label="modal")
                for _ in prompts]

    import scout.llm.qwen as qwen
    monkeypatch.setattr(qwen, "complete_batch", complete_batch)
    return calls


def test_private_goes_only_to_qwen(monkeypatch, fake_qwen):
    def boom(*a, **k):
        raise AssertionError("private data was about to be sent to Google")

    monkeypatch.setattr(gemini, "run_parallel", boom)
    monkeypatch.setattr(gemini, "run_job", boom)
    r = router.complete("note", "my private note", schema={"type": "object"}, private=True)
    assert r.model_id.startswith("Qwen/") and fake_qwen == [["my private note"]]


def test_public_uses_gemini_and_never_qwen_when_gemini_works(monkeypatch, fake_qwen):
    monkeypatch.setattr(gemini, "run_parallel", lambda tasks, workers=None: [
        Result(text="{}", data={}, model_id="gemini-4-hydrogen", key_label="main") for _ in tasks])
    r = router.complete("card", "public news", schema={"type": "object"})
    assert r.model_id == "gemini-4-hydrogen" and fake_qwen == []


def test_public_falls_back_to_qwen_only_for_failed_prompts(monkeypatch, fake_qwen):
    def run_parallel(tasks, workers=None):
        return [Result(text="{}", data={}, model_id="gemini-4-hydrogen", key_label="main"),
                GeminiUnavailable([("main", GeminiError("quota", "x"))])]

    monkeypatch.setattr(gemini, "run_parallel", run_parallel)
    out = router.complete_many("score", ["a", "b"], schema={"type": "object"})
    assert out[0].model_id == "gemini-4-hydrogen" and out[1].model_id.startswith("Qwen/")
    assert fake_qwen == [["b"]]


def test_forced_gemini_failure_uses_qwen(monkeypatch, fake_qwen):
    monkeypatch.setenv("FORCE_GEMINI_FAIL", "1")
    monkeypatch.setattr(gemini, "run_parallel", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    out = router.complete_many("card", ["x"], schema={"type": "object"})
    assert out[0].model_id.startswith("Qwen/")


def test_fact_gate_drops_sentences_with_unsourced_names_or_numbers():
    from scout import cards
    material = "End Close, an Irish-founded startup, raised EUR 3.2m (€3.2m) from Frontline to fix payment mismatches."
    card = {"what_they_do": "End Close is an Irish-founded, Y Combinator-backed startup. It fixes payment mismatches.",
            "why_funded": "It raised €3.2m from Frontline. It has 500 customers.",
            "bangladesh_version": "Ideas stay untouched, even Sequoia."}
    fixed, removed = cards.fact_gate(card, material, "End Close", "Fix payment mismatches")
    assert fixed["what_they_do"] == "It fixes payment mismatches."
    assert fixed["why_funded"] == "It raised €3.2m from Frontline."
    assert fixed["bangladesh_version"] == card["bangladesh_version"]
    assert len(removed) == 2 and "Combinator" in removed[0] and "500" in removed[1]


def test_fact_gate_keeps_supported_sentences_and_falls_back():
    from scout import cards
    card = {"what_they_do": "Rezora answers calls in English or Spanish for HVAC firms.", "why_funded": "unknown"}
    fixed, removed = cards.fact_gate(card, "Rezora AI agents answer calls 24/7 in English or Spanish. HVAC, plumbing.",
                                     "Rezora IO", None)
    assert removed == [] and fixed == card
    fixed, removed = cards.fact_gate({"what_they_do": "Backed by Sequoia.", "why_funded": ""}, "nothing", "X", "A tool")
    assert fixed["what_they_do"] == "A tool" and fixed["why_funded"] == "unknown"
