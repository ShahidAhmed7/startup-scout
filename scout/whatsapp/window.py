"""The WhatsApp 24-hour customer service window.

Free-form messages are only allowed within 24 hours of the owner's last message.
We treat the window as open only if that message is less than 23h 50m old.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scout.config import config

CLOCK_SKEW = timedelta(minutes=5)


def window_length() -> timedelta:
    return timedelta(minutes=config()["whatsapp"]["window_minutes"])


def is_open(last_owner_message_at: datetime | None, now: datetime | None = None) -> bool:
    if last_owner_message_at is None:
        return False
    if last_owner_message_at.tzinfo is None:
        raise ValueError("last_owner_message_at must be timezone-aware")
    now = now or datetime.now(timezone.utc)
    age = now - last_owner_message_at
    return -CLOCK_SKEW <= age < window_length()


def closes_at(last_owner_message_at: datetime | None) -> datetime | None:
    if last_owner_message_at is None:
        return None
    return last_owner_message_at + window_length()
