from scout.config import SOURCE_KINDS, SOURCE_STATUSES, config, sources


def test_config_has_required_sections():
    cfg = config()
    for key in ("timezone", "digest_mix", "skip_list", "weights", "models", "caps", "budget", "whatsapp"):
        assert key in cfg
    assert cfg["output_language"] == "English"
    assert cfg["caps"]["gemini_flash_per_day"] < cfg["gemini_limits"]["flash"]["rpd"]
    assert cfg["caps"]["gemini_flash_lite_per_day"] < cfg["gemini_limits"]["flash_lite"]["rpd"]
    assert cfg["budget"]["modal_warn_usd"] < cfg["budget"]["modal_stop_usd"] <= 25


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
