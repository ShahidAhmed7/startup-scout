from scout.config import SOURCE_KINDS, SOURCE_STATUSES, config, sources


def test_config_routing_and_scoring():
    cfg = config()
    assert cfg["jobs"]["score"][0] == ["strong_main", "strong_backup"]
    assert cfg["jobs"]["private"] == [["private"]]
    assert cfg["min_score"] == 6.5 and cfg["fill_min_score"] == 5
    assert cfg["budget"]["modal_stop_usd"] <= 30  # Modal Starter gives $30 of free credit a month
    for key in ("weights", "owner", "ai_angle", "min_feasibility"):
        assert key not in cfg
    for chain in list(cfg["jobs"].values()) + list(cfg["dev_jobs"].values()):
        assert all(entry in cfg["models"] for group in chain for entry in group)


def test_sources_are_well_formed():
    all_sources = sources()
    ids = [s["id"] for s in all_sources]
    assert len(ids) == len(set(ids)), "duplicate source id"
    for s in all_sources:
        assert s["kind"] in SOURCE_KINDS, s["id"]
        assert s["status"] in SOURCE_STATUSES, s["id"]
        assert s["role"] in {"funding", "context"}, s["id"]
        assert s["priority"] in {"high", "normal"}, s["id"]
        assert s["url"], s["id"]


def test_yc_and_us_sources_are_high_priority_and_active():
    by_id = {s["id"]: s for s in sources()}
    for sid in ("yc_oss", "hn_launch", "techcrunch_venture", "crunchbase_news"):
        assert by_id[sid]["priority"] == "high" and by_id[sid]["status"] == "active"


def test_every_requested_region_has_an_active_source():
    regions = {s["region"] for s in sources("active")}
    for r in (
        "usa",
        "india",
        "pakistan",
        "mideast",
        "africa",
        "sea",
        "china",
        "bangladesh",
        "europe",
    ):
        assert r in regions, r


def test_prompts_have_no_owner_profile_bias():
    import inspect
    from scout.llm import prompts

    text = inspect.getsource(prompts).lower()
    for term in ("skills", "2-3", "small team", "field is ai", "fits_you"):
        assert term not in text
