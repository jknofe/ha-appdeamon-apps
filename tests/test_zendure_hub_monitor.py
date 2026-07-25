"""Tests for ZendureHubMonitor's pure functions.

The firmware-init split is the fix for a confirmed hardware bug: the hub
silently drops multi-property MQTT writes, so the combined
{minSoc, passMode, outputLimit} payload never applied.
"""
from ZendureHubMonitor import (
    MIN_SOC_SCALE,
    bypass_status,
    firmware_init_payloads,
    is_bypass_active,
)


# ----------------------------------------------------------------------
# firmware_init_payloads - the multi-property write fix
# ----------------------------------------------------------------------

def test_each_payload_carries_exactly_one_property():
    # The whole point of the fix: the hub ignores any payload with more than
    # one property, so a regression here silently stops reaching the hardware.
    for payload in firmware_init_payloads(10, 0):
        assert len(payload["properties"]) == 1


def test_min_soc_is_scaled_to_tenths_of_percent():
    payloads = firmware_init_payloads(10, 0)
    assert {"properties": {"minSoc": 100}} in payloads


def test_min_soc_scale_constant_matches():
    assert firmware_init_payloads(20, 0)[0]["properties"]["minSoc"] == 20 * MIN_SOC_SCALE


def test_pass_mode_is_sent_unscaled():
    payloads = firmware_init_payloads(10, 1)
    assert {"properties": {"passMode": 1}} in payloads


def test_output_limit_is_not_sent():
    # Dropped deliberately: it never reached the hub before (it rode along in
    # the ignored combined payload), and sending it now would introduce a real
    # discharge interruption on every restart.
    for payload in firmware_init_payloads(10, 0):
        assert "outputLimit" not in payload["properties"]


def test_exactly_two_payloads():
    assert len(firmware_init_payloads(10, 0)) == 2


def test_payloads_have_the_properties_envelope():
    for payload in firmware_init_payloads(10, 0):
        assert list(payload) == ["properties"]


# ----------------------------------------------------------------------
# Pre-existing pure functions - previously untested (see .ai/notes.md)
# ----------------------------------------------------------------------

def test_bypass_active_all_conditions_met():
    assert is_bypass_active(100, "idle", 0, 60, 50) is True


def test_bypass_inactive_when_soc_below_100():
    assert is_bypass_active(99, "idle", 0, 60, 50) is False


def test_bypass_inactive_when_not_idle():
    assert is_bypass_active(100, "discharging", 0, 60, 50) is False


def test_bypass_inactive_when_pack_power_nonzero():
    assert is_bypass_active(100, "idle", 5, 60, 50) is False


def test_bypass_solar_threshold_is_strict():
    # Irradiance noise hovers at the threshold in low light; >= would flap.
    assert is_bypass_active(100, "idle", 0, 50, 50) is False
    assert is_bypass_active(100, "idle", 0, 51, 50) is True


def test_bypass_status_combinations():
    assert bypass_status(True, True) == "both"
    assert bypass_status(True, False) == "app_only"
    assert bypass_status(False, True) == "zendure_only"
    assert bypass_status(False, False) == "none"
