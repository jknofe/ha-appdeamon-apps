"""Tests for app_helpers.parse_interval."""
import pytest

from app_helpers import parse_interval


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
