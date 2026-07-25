"""Tests for app_helpers."""
import pytest

from app_helpers import parse_interval, publish_log_action, publish_succeeded


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


# ---- publish_succeeded ----
# Shapes taken from AppDaemon 4.5.13 hassplugin.websocket_send_json.

def test_publish_succeeded_on_ok_result():
    assert publish_succeeded({"success": True, "ad_status": "OK", "ad_duration": 0.01}) is True


def test_publish_succeeded_false_on_hass_error():
    # What was actually logged during the 2026-07-25 Mosquitto restart.
    result = {"success": False, "error": {"code": "home_assistant_error",
                                          "message": "Error talking to MQTT: "
                                                     "The client is not currently connected."}}
    assert publish_succeeded(result) is False


def test_publish_succeeded_false_on_timeout():
    assert publish_succeeded({"success": False, "ad_status": "TIMEOUT"}) is False


def test_publish_succeeded_false_on_none():
    # websocket_send_json returns None when the send is skipped during shutdown.
    assert publish_succeeded(None) is False


def test_publish_succeeded_false_on_missing_success_key():
    assert publish_succeeded({"ad_status": "OK"}) is False


def test_publish_succeeded_false_on_non_dict():
    assert publish_succeeded("ok") is False
    assert publish_succeeded(True) is False


def test_publish_succeeded_requires_exactly_true():
    # Truthy is not good enough - only an explicit True means HA acknowledged.
    assert publish_succeeded({"success": 1}) is False


# ---- publish_log_action ----

def test_log_action_first_failure_logs_error():
    assert publish_log_action(False, False) == ("error", True)


def test_log_action_repeated_failure_is_silent():
    # A broker outage must not log once per 20 s tick.
    assert publish_log_action(False, True) == (None, True)


def test_log_action_recovery_logs_once():
    assert publish_log_action(True, True) == ("recovered", False)


def test_log_action_steady_success_is_silent():
    assert publish_log_action(True, False) == (None, False)


def test_log_action_full_outage_cycle():
    failing = False
    actions = []
    for ok in (True, False, False, False, True, True):
        action, failing = publish_log_action(ok, failing)
        actions.append(action)
    assert actions == [None, "error", None, None, "recovered", None]
