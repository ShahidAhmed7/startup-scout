from datetime import datetime, timedelta, timezone

import pytest

from scout.whatsapp.window import closes_at, is_open

NOW = datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)  # 08:00 Dhaka


def test_never_messaged_is_closed():
    assert is_open(None, NOW) is False


def test_just_messaged_is_open():
    assert is_open(NOW - timedelta(minutes=1), NOW)


def test_open_just_before_23h50():
    assert is_open(NOW - timedelta(hours=23, minutes=49, seconds=59), NOW)


def test_closed_at_23h50_even_though_meta_allows_24h():
    assert not is_open(NOW - timedelta(hours=23, minutes=50), NOW)
    assert not is_open(NOW - timedelta(hours=23, minutes=55), NOW)


def test_closed_after_a_day():
    assert not is_open(NOW - timedelta(days=2), NOW)


def test_small_clock_skew_is_tolerated_but_far_future_is_not():
    assert is_open(NOW + timedelta(minutes=2), NOW)
    assert not is_open(NOW + timedelta(hours=1), NOW)


def test_other_timezones_compare_correctly():
    dhaka = timezone(timedelta(hours=6))
    last = datetime(2026, 11, 18, 9, 0, tzinfo=dhaka)  # 03:00 UTC yesterday -> 23h old
    assert is_open(last, NOW)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        is_open(datetime(2026, 11, 19, 1, 0), NOW)


def test_closes_at():
    last = NOW - timedelta(hours=1)
    assert closes_at(last) == last + timedelta(hours=23, minutes=50)
    assert closes_at(None) is None
