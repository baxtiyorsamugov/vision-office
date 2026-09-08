"""Timezone conversion for operator-facing Vision Office timestamps."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.edge.config import load_edge_settings


DEFAULT_TIMEZONE = "Asia/Tashkent"


def device_timezone() -> ZoneInfo:
    """Return the configured Edge timezone without making UI rendering fragile."""
    try:
        configured_name = load_edge_settings().timezone or DEFAULT_TIMEZONE
        return ZoneInfo(configured_name)
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def as_utc(value: datetime) -> datetime:
    """Interpret legacy naive database values as UTC and normalize aware ones."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_local(value: datetime) -> datetime:
    return as_utc(value).astimezone(device_timezone())


def format_local(value: datetime | None, pattern: str = "%d.%m.%Y %H:%M:%S") -> str:
    return to_local(value).strftime(pattern) if value is not None else "—"


def local_now() -> datetime:
    return datetime.now(device_timezone())


def local_today() -> date:
    return local_now().date()


def local_day_bounds_utc(selected_date: date) -> tuple[datetime, datetime]:
    """Return a local calendar day as naive UTC bounds for legacy attendance rows."""
    start = datetime.combine(selected_date, time.min, tzinfo=device_timezone())
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def local_time_to_utc(selected_date: date, selected_time: time) -> datetime:
    value = datetime.combine(selected_date, selected_time, tzinfo=device_timezone())
    return value.astimezone(timezone.utc).replace(tzinfo=None)
