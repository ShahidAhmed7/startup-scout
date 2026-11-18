from scout.config import SOURCE_KINDS, SOURCE_STATUSES, config, sources


def test_config_has_required_sections():
    cfg = config()
    for key in ("timezone", "digest_mix", "skip_list", "weights", "models", "fallback", "llm", "budget", "whatsapp"):
        assert key in cfg
    assert cfg["output_language"] == "English"
    assert cfg["budget"]["modal_warn_usd"] < cfg["budget"]["modal_stop_usd"] <= 25


def test_owner_model_choices():
    m = config()["models"]
    assert m["card"]["id"] == "gemini-4-hydrogen"
    assert m["flash_lite"]["id"] == "gemini-3.5-flash-lite"
    assert m["gemma"]["id"] == "gemma-4-31b-it"
    assert config()["fallback"]["card"] == ["flash_lite", "gemma"]
    assert config()["llm"]["thinking_level"] == "high"


def test_our_caps_stay_below_free_limits():
    for alias in ("card", "flash_lite", "gemma"):
        mc = config()["models"][alias]
        assert mc["cap_per_day"] < mc["free_limits"]["rpd"], alias


def test_fallbacks_point_at_real_models():
    models = config()["models"]
    for alias, chain in config()["fallback"].items():
        assert alias in models
        assert all(a in models and a != "qwen" for a in chain), "Qwen is reached by the router, not the chain"


def test_input_budget_fits_free_tier_tokens_per_minute():
    cfg = config()
    assert cfg["llm"]["max_input_tokens"] <= cfg["models"]["card"]["free_limits"]["tpm"]


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
    for r in ("usa", "india", "pakistan", "mideast", "africa", "sea", "china", "bangladesh", "europe"):
        assert r in regions, r
