"""Common helpers for AppDaemon apps in this repo.

Usage:
    from app_helpers import parse_interval, next_aligned_minute
"""
import datetime
import re


_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([a-zA-Z]*)\s*$")
_UNIT_FACTORS = {
    "s":    1,
    "sec":  1,
    "secs": 1,
    "m":    60,
    "min":  60,
    "mins": 60,
    "h":    3600,
    "hr":   3600,
    "hrs":  3600,
}


def parse_interval(value):
    """Parse a duration spec into seconds (int).

    Accepted forms (case- and whitespace-insensitive):
        20, "20"            -> 20         (bare number = seconds)
        "20s", "20sec"      -> 20
        "20m", "20min"      -> 1200
        "2h", "2hr"         -> 7200

    Raises ValueError on anything else.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"interval must be int or string, got {type(value).__name__}")
    m = _INTERVAL_RE.match(value)
    if not m:
        raise ValueError(f"unrecognised interval: {value!r}")
    unit = m.group(2).lower() or "s"
    if unit not in _UNIT_FACTORS:
        raise ValueError(f"unknown unit {unit!r} in interval {value!r}")
    return int(m.group(1)) * _UNIT_FACTORS[unit]


def next_aligned_minute(now, interval_min):
    """Next datetime strictly after `now` whose minute is a multiple of `interval_min`.

    Useful for anchoring run_every schedules to clock boundaries — e.g. with
    interval_min=20 the result is one of :00, :20, :40 every hour.

    `interval_min` must be in [1, 60] and divide 60 evenly so the boundaries
    repeat cleanly each hour. The result preserves now's tzinfo.
    """
    if interval_min < 1 or interval_min > 60 or 60 % interval_min != 0:
        raise ValueError(f"interval_min={interval_min} must divide 60 evenly")
    next_minute = ((now.minute // interval_min) + 1) * interval_min
    if next_minute >= 60:
        return (now + datetime.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
    return now.replace(minute=next_minute, second=0, microsecond=0)
