import pytest

from scout.whatsapp.formats import LIMITS, buttons_payload, clip, list_payload, row_title, split_text


def test_clip_cuts_at_word_boundary_and_respects_limit():
    s = "Startup Scout reads funding news every morning and scores it"
    out = clip(s, 30)
    assert len(out) <= 30 and out.endswith("…") and not out[:-1].endswith(" ")
    assert clip("short", 30) == "short"


def test_clip_never_exceeds_limit_even_without_spaces():
    assert len(clip("x" * 100, 24)) <= 24


def test_list_payload_enforces_all_limits():
    rows = [{"id": f"card:{i}", "title": "A very long startup name indeed · BD 8",
             "description": "word " * 40} for i in range(14)]
    p = list_payload("880", "b" * 2000, "See all the picks now please", [{"title": "For Bangladesh and beyond!!", "rows": rows}])
    inter = p["interactive"]
    assert len(inter["body"]["text"]) <= LIMITS["body"]
    assert len(inter["action"]["button"]) <= LIMITS["list_button"]
    sec = inter["action"]["sections"][0]
    assert len(sec["title"]) <= LIMITS["section_title"]
    assert len(sec["rows"]) == 10
    assert all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in sec["rows"])


def test_rows_spread_over_sections_still_max_10():
    rows = [{"id": str(i), "title": f"T{i}"} for i in range(7)]
    p = list_payload("880", "b", "See picks", [{"title": "A", "rows": rows}, {"title": "B", "rows": rows}])
    assert sum(len(s["rows"]) for s in p["interactive"]["action"]["sections"]) == 10


def test_buttons_limits():
    p = buttons_payload("880", "What next?", [("save:1", "Save"), ("skip:1", "Skip"), ("more:1", "More about this one")])
    titles = [b["reply"]["title"] for b in p["interactive"]["action"]["buttons"]]
    assert all(len(t) <= 20 for t in titles)
    with pytest.raises(ValueError):
        buttons_payload("880", "x", [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")])


def test_row_title_keeps_score_and_fits():
    t = row_title("Extraordinarily Long Company Name", "Global 9")
    assert len(t) <= 24 and t.endswith(" · Global 9")
    assert row_title("Render", "BD 8") == "Render · BD 8"


def test_split_text_keeps_everything_under_limit():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 300 for i in range(6))
    parts = split_text(text)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert sum(len(p.split()) for p in parts) == len(text.split())


def test_numbered_lines():
    from scout.whatsapp.formats import numbered_lines
    assert numbered_lines("1. Post in groups. 2. Call 5 clinics. 3. Count replies.") == \
        "1. Post in groups.\n2. Call 5 clinics.\n3. Count replies."
    assert numbered_lines("Just one sentence.") == "Just one sentence."
    assert numbered_lines("1. a\n2. b") == "1. a\n2. b"


def test_company_suffixes_are_ignored_when_merging():
    from scout.normalize import norm_name
    assert norm_name("Genesis AI SAS") == norm_name("Genesis AI") == "genesis ai"
    assert norm_name("Isomorphic Labs Ltd.") == "isomorphic labs"
    assert norm_name("Orikata Bio PBC") == "orikata bio"
