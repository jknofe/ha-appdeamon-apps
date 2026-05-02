"""Tests for app_helpers.parse_interval and next_aligned_minute."""
import datetime

import pytest

from app_helpers import next_aligned_minute, parse_interval


# ---- bare numeric forms ----

def test_parse_int_seconds():
    assert parse_interval(20) == 20
    assert parse_interval(0) == 0
    assert parse_interval(1200) == 1200


def test_parse_string_no_unit_treated_as_seconds():
    assert parse_interval("20") == 20


# ---- seconds suffixes ----

def test_parse_seconds_short():
    assert parse_interval("20s") == 20


def test_parse_seconds_long():
    assert parse_interval("20sec") == 20
    assert parse_interval("20secs") == 20


# ---- minutes ----

def test_parse_minutes_short_m():
    assert parse_interval("20m") == 20 * 60


def test_parse_minutes_long():
    assert parse_interval("20min") == 20 * 60
    assert parse_interval("5mins") == 5 * 60


# ---- hours ----

def test_parse_hours():
    assert parse_interval("1h") == 3600
    assert parse_interval("2hr") == 7200
    assert parse_interval("3hrs") == 10800


# ---- case + whitespace tolerance ----

def test_parse_case_insensitive():
    assert parse_interval("20S") == 20
    assert parse_interval("20MIN") == 20 * 60
    assert parse_interval("1H") == 3600


def test_parse_whitespace_tolerated():
    assert parse_interval("  20s  ") == 20
    assert parse_interval("20 s") == 20


# ---- error cases ----

def test_parse_unknown_unit_raises():
    with pytest.raises(ValueError):
        parse_interval("20x")


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        parse_interval("not a number")


def test_parse_negative_raises():
    # regex requires \d+ (no leading minus); negative duration is meaningless.
    with pytest.raises(ValueError):
        parse_interval("-20s")


def test_parse_none_raises():
    with pytest.raises(ValueError):
        parse_interval(None)


def test_parse_empty_string_raises():
    with pytest.raises(ValueError):
        parse_interval("")


# ---- next_aligned_minute ----

def _dt(h, m, s=0, us=0):
    return datetime.datetime(2026, 5, 2, h, m, s, us)


def test_aligned_mid_interval_advances_to_next_boundary():
    assert next_aligned_minute(_dt(15, 14, 30), 20) == _dt(15, 20)


def test_aligned_just_before_boundary():
    assert next_aligned_minute(_dt(15, 19, 59, 999_999), 20) == _dt(15, 20)


def test_aligned_exactly_on_boundary_advances():
    # On-boundary input must produce the *next* boundary, not the current one.
    # Returning current would mean a tick fires at AppDaemon-init time + ~0s,
    # which collides with the run_in(_tick, 1) kickoff.
    assert next_aligned_minute(_dt(15, 20, 0), 20) == _dt(15, 40)


def test_aligned_rolls_to_next_hour():
    assert next_aligned_minute(_dt(15, 40), 20) == _dt(16, 0)


def test_aligned_just_after_top_of_hour():
    assert next_aligned_minute(_dt(15, 0, 1), 20) == _dt(15, 20)


def test_aligned_15min_interval():
    assert next_aligned_minute(_dt(15, 7), 15) == _dt(15, 15)
    assert next_aligned_minute(_dt(15, 45), 15) == _dt(16, 0)


def test_aligned_60min_interval():
    assert next_aligned_minute(_dt(15, 30), 60) == _dt(16, 0)


def test_aligned_invalid_zero_raises():
    with pytest.raises(ValueError):
        next_aligned_minute(_dt(15, 0), 0)


def test_aligned_invalid_non_divisor_raises():
    with pytest.raises(ValueError):
        next_aligned_minute(_dt(15, 0), 13)


def test_aligned_preserves_tzinfo():
    tz = datetime.timezone(datetime.timedelta(hours=2))
    now = datetime.datetime(2026, 5, 2, 15, 14, tzinfo=tz)
    result = next_aligned_minute(now, 20)
    assert result.tzinfo == tz
    assert result == datetime.datetime(2026, 5, 2, 15, 20, tzinfo=tz)
