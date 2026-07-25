"""Common helpers for AppDaemon apps in this repo.

Usage:
    from app_helpers import parse_interval, publish_succeeded
"""
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


def publish_succeeded(result):
    """Was a call_service("mqtt/publish", ...) actually delivered?

    AppDaemon never raises on a failed service call - it returns a result dict
    and logs a warning on its own logger. So an unchecked call_service cannot
    tell a delivered publish from a dropped one; the caller must inspect the
    return value. Verified against AppDaemon 4.5.13:
    hassplugin.websocket_send_json returns {"success": bool, "ad_status": ...},
    with success False on both a HASS error and a timeout, and returns None
    when the send is skipped entirely (websocket closed during shutdown).
    """
    if not isinstance(result, dict):
        return False
    return result.get("success") is True


def publish_log_action(ok, was_failing):
    """Decide what to log for a publish result. Returns (action, now_failing)
    where action is 'error' on the first failure of a run, 'recovered' on the
    first success after failures, else None.

    Without this, a broker outage would log once per tick - every 20 s for the
    setpoint loop - for as long as it lasts.
    """
    if not ok:
        return (None if was_failing else "error"), True
    return ("recovered" if was_failing else None), False
