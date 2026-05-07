"""Unit tests for zendure_logic pure functions.

Test names map to TST-N IDs in zendure-requirements.md. Each test docstring
cites the specific TST-N + REQ ID it covers so coverage is traceable.
"""
import pytest

from zendure_logic import (
    battery_discharged_latch,
    bypass_status,
    compute_setpoint,
    derive_bypass_now,
    derive_hm400_from_shelly,
    effective_batt_low_stop,
    force_weekly_charge,
    is_bypass_active,
    pick_mode_payload,
    pick_operation_mode,
    refine_active_mode,
)


# Reference 24-slot schedule per SM-4 default:
# 00..05 serve, 06..07 charge, 08..14 dual, 15..23 serve.
DEFAULT_SCHEDULE = (
    ['serve'] * 6
    + ['charge'] * 2
    + ['dual'] * 7
    + ['serve'] * 9
)
assert len(DEFAULT_SCHEDULE) == 24  # sanity guard for the test fixture


# ============================================================================
# is_bypass_active (BT-4) — TST-1..6
# ============================================================================

def test_bypass_all_conditions_met():
    """TST-1 / BT-4: all four conditions met -> True."""
    assert is_bypass_active(
        electric_level=100, packstate='idle',
        outputpackpower=0, solarinputpower=100, solar_threshold=50,
    ) is True


def test_bypass_level_99_false():
    """TST-2 / BT-4: electric_level == 99 -> False."""
    assert is_bypass_active(
        electric_level=99, packstate='idle',
        outputpackpower=0, solarinputpower=100, solar_threshold=50,
    ) is False


def test_bypass_packstate_charging_false():
    """TST-3 / BT-4: packstate == 'charging' -> False."""
    assert is_bypass_active(
        electric_level=100, packstate='charging',
        outputpackpower=0, solarinputpower=100, solar_threshold=50,
    ) is False


def test_bypass_outputpackpower_nonzero_false():
    """TST-4 / BT-4: outputpackpower == 1 -> False."""
    assert is_bypass_active(
        electric_level=100, packstate='idle',
        outputpackpower=1, solarinputpower=100, solar_threshold=50,
    ) is False


def test_bypass_solar_at_threshold_false():
    """TST-5 / BT-4: solarinputpower == solar_threshold (strict >) -> False."""
    assert is_bypass_active(
        electric_level=100, packstate='idle',
        outputpackpower=0, solarinputpower=50, solar_threshold=50,
    ) is False


def test_bypass_solar_just_above_threshold_true():
    """TST-6 / BT-4: solarinputpower == solar_threshold + 1 -> True."""
    assert is_bypass_active(
        electric_level=100, packstate='idle',
        outputpackpower=0, solarinputpower=51, solar_threshold=50,
    ) is True


# ============================================================================
# bypass_status (BT-7) — TST-37..40
# ============================================================================

def test_bypass_status_none():
    """TST-37 / BT-7: neither active -> 'none'."""
    assert bypass_status(False, False) == "none"


def test_bypass_status_app_only():
    """TST-38 / BT-7: app says yes, Zendure silent -> 'app_only' (the case we
    work around when Zendure stops reporting `pass`)."""
    assert bypass_status(True, False) == "app_only"


def test_bypass_status_zendure_only():
    """TST-39 / BT-7: Zendure reports yes, our predicate disagrees ->
    'zendure_only' (would warrant a predicate review if seen often)."""
    assert bypass_status(False, True) == "zendure_only"


def test_bypass_status_both():
    """TST-40 / BT-7: both agree -> 'both'."""
    assert bypass_status(True, True) == "both"


# ============================================================================
# pick_operation_mode (SM-4, SM-5) — TST-7..10
# ============================================================================

def test_pick_mode_serve_morning():
    """TST-7 / SM-4: hours 0, 5 -> serve."""
    assert pick_operation_mode(0, DEFAULT_SCHEDULE) == 'serve'
    assert pick_operation_mode(5, DEFAULT_SCHEDULE) == 'serve'


def test_pick_mode_charge_window():
    """TST-8 / SM-4: hours 6, 7 -> charge."""
    assert pick_operation_mode(6, DEFAULT_SCHEDULE) == 'charge'
    assert pick_operation_mode(7, DEFAULT_SCHEDULE) == 'charge'


def test_pick_mode_dual_window():
    """TST-9 / SM-4: hours 8, 14 -> dual."""
    assert pick_operation_mode(8, DEFAULT_SCHEDULE) == 'dual'
    assert pick_operation_mode(14, DEFAULT_SCHEDULE) == 'dual'


def test_pick_mode_serve_evening():
    """TST-10 / SM-4: hours 15, 23 -> serve."""
    assert pick_operation_mode(15, DEFAULT_SCHEDULE) == 'serve'
    assert pick_operation_mode(23, DEFAULT_SCHEDULE) == 'serve'


# ============================================================================
# pick_mode_payload (SM-7..15) — TST-11..22
# ============================================================================

LOW = 100   # low_minsoc default per CFG-4 (=10%)
MED = 200   # med_minsoc default per CFG-4 (=20%)


def test_pick_payload_no_change_no_bypass():
    """TST-11 / SM-15: old=serve, new=serve, no bypass -> (None, serve)."""
    payload, mode = pick_mode_payload(
        old_mode='serve', new_mode='serve', bypass_now=False,
        electric_level=50, days_since_last_bypass=2,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'serve'


def test_pick_payload_no_change_with_bypass():
    """TST-12 / SM-14: no mode change but bypass_now -> bypass-low-stop payload."""
    payload, mode = pick_mode_payload(
        old_mode='serve', new_mode='serve', bypass_now=True,
        electric_level=100, days_since_last_bypass=0,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload == {'properties': {'outputLimit': 0, 'passMode': 0, 'minSoc': LOW}}
    assert mode == 'serve'


def test_pick_payload_to_serve_with_bypass():
    """TST-13 / SM-8: charge->serve with bypass_now -> outputLimit:0, passMode:1, minSoc:low."""
    payload, mode = pick_mode_payload(
        old_mode='charge', new_mode='serve', bypass_now=True,
        electric_level=100, days_since_last_bypass=0,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload == {'properties': {'outputLimit': 0, 'passMode': 1, 'minSoc': LOW}}
    assert mode == 'serve'


def test_pick_payload_to_serve_30pct_recent_bypass():
    """TST-14 / SM-9: charge->serve, level>=30 and days<7 -> outputLimit:0, minSoc:med."""
    payload, mode = pick_mode_payload(
        old_mode='charge', new_mode='serve', bypass_now=False,
        electric_level=30, days_since_last_bypass=3,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload == {'properties': {'outputLimit': 0, 'minSoc': MED}}
    assert mode == 'serve'


def test_pick_payload_to_serve_low_level_delays():
    """TST-15 / SM-10: charge->serve, level<30 (no bypass, days<7) -> delay (None, charge)."""
    payload, mode = pick_mode_payload(
        old_mode='charge', new_mode='serve', bypass_now=False,
        electric_level=29, days_since_last_bypass=3,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'charge'


def test_pick_payload_to_serve_old_bypass_delays():
    """TST-16 / SM-10: charge->serve, level=30 but days>=7 -> delay (None, charge)."""
    payload, mode = pick_mode_payload(
        old_mode='charge', new_mode='serve', bypass_now=False,
        electric_level=30, days_since_last_bypass=7,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'charge'


def test_pick_payload_to_dual_low_level_recent_delays():
    """TST-17 / SM-11: charge->dual, level<20 and days<7 -> delay (None, charge)."""
    payload, mode = pick_mode_payload(
        old_mode='charge', new_mode='dual', bypass_now=False,
        electric_level=19, days_since_last_bypass=3,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'charge'


def test_pick_payload_to_dual_healthy_advances():
    """TST-18 / SM-12: charge->dual, level>=20 -> advance, no payload."""
    payload, mode = pick_mode_payload(
        old_mode='charge', new_mode='dual', bypass_now=False,
        electric_level=20, days_since_last_bypass=3,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'dual'


def test_pick_payload_to_dual_old_bypass_advances():
    """TST-19 / SM-12: charge->dual, level<20 but days>=7 -> advance (delay needs both)."""
    payload, mode = pick_mode_payload(
        old_mode='charge', new_mode='dual', bypass_now=False,
        electric_level=19, days_since_last_bypass=7,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'dual'


def test_pick_payload_to_charge_emits_payload():
    """TST-20 / SM-13: serve->charge always emits charge payload."""
    payload, mode = pick_mode_payload(
        old_mode='serve', new_mode='charge', bypass_now=False,
        electric_level=50, days_since_last_bypass=3,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload == {'properties': {'outputLimit': 0, 'passMode': 0, 'minSoc': LOW}}
    assert mode == 'charge'


def test_pick_payload_unknown_old_mode():
    """TST-21 / SM-7: old='unknown' -> treated as same-as-new, no transition."""
    payload, mode = pick_mode_payload(
        old_mode='unknown', new_mode='charge', bypass_now=False,
        electric_level=50, days_since_last_bypass=3,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'charge'


def test_pick_payload_none_old_mode():
    """TST-22 / SM-7: old=None -> treated as same-as-new, no transition."""
    payload, mode = pick_mode_payload(
        old_mode=None, new_mode='dual', bypass_now=False,
        electric_level=50, days_since_last_bypass=3,
        low_minsoc=LOW, med_minsoc=MED,
    )
    assert payload is None
    assert mode == 'dual'


# ============================================================================
# derive_bypass_now (SP-4) — TST-23..26
# ============================================================================

def test_derive_bypass_now_idle_zero_power():
    """TST-23 / SP-4: (0, 'idle') -> True."""
    assert derive_bypass_now(outputpackpower=0, packstate='idle') is True


def test_derive_bypass_now_charging():
    """TST-24 / SP-4: (0, 'charging') -> False."""
    assert derive_bypass_now(outputpackpower=0, packstate='charging') is False


def test_derive_bypass_now_discharging():
    """TST-25 / SP-4: (0, 'discharging') -> False."""
    assert derive_bypass_now(outputpackpower=0, packstate='discharging') is False


def test_derive_bypass_now_nonzero_power():
    """TST-26 / SP-4: (50, 'idle') -> False."""
    assert derive_bypass_now(outputpackpower=50, packstate='idle') is False


# ============================================================================
# compute_setpoint (SP-5..11) — TST-27..36
# ============================================================================

# Standard "non-blocking" args for serve mode: high SoC, no battery protection,
# default caps. Used as the base; tests override only what they care about.
def _serve_args(**overrides):
    base = dict(
        power_con=0, power_sol=0, mode='serve', solar_input_power=0,
        electric_level=50, batt_low_stop=10,
        dual_cap=720, serve_cap=540,
        power_step=30, target_bias_steps=0.5,
    )
    base.update(overrides)
    return base


def test_compute_serve_quantizes_to_step_with_bias():
    """TST-27 / SP-5,6: serve mode, target=300-15=285, quantizes to 270 (within cap)."""
    sp = compute_setpoint(**_serve_args(power_con=300))
    # target = 300 - 0 - (30 * 0.5) = 285; (285 // 30) * 30 = 270
    assert sp == 270


def test_compute_serve_clamps_to_serve_cap():
    """TST-28 / SP-9,11: large target in serve clamped at serve_cap=540."""
    sp = compute_setpoint(**_serve_args(power_con=1000))
    # target = 1000 - 15 = 985; quantized = 960; clamped at 540
    assert sp == 540


def test_compute_serve_negative_target_clamps_to_zero():
    """TST-29 / SP-11: target negative -> 0."""
    sp = compute_setpoint(**_serve_args(power_con=0, power_sol=0))
    # target = -15, quantized negative, clamped at 0
    assert sp == 0


def test_compute_charge_mode_forces_zero():
    """TST-30 / SP-7: charge mode -> 0 regardless of inputs."""
    sp = compute_setpoint(**_serve_args(mode='charge', power_con=500, power_sol=0))
    assert sp == 0


def test_compute_dual_tracks_target_below_cap():
    """TST-31 / SP-8: dual caps at dual_cap=720; target below cap passes through quantized."""
    # power_con=500: target=485, quantized=480; min(480, 720) = 480
    sp = compute_setpoint(**_serve_args(mode='dual', power_con=500))
    assert sp == 480


def test_compute_dual_independent_of_solar():
    """TST-32 / SP-8: dual no longer applies a solar-input cap; output tracks target only."""
    # solar=50 used to make setpoint=0 via half_solar; now it doesn't matter.
    sp = compute_setpoint(**_serve_args(mode='dual', power_con=500, solar_input_power=50))
    assert sp == 480


def test_compute_dual_clamps_to_dual_cap():
    """TST-33 / SP-8: dual with target far above cap clamps at dual_cap=720."""
    # power_con=1000, target=985, quantized=960; min(960, 720) = 720
    sp = compute_setpoint(**_serve_args(mode='dual', power_con=1000))
    assert sp == 720


def test_compute_battery_protection_at_low_stop():
    """TST-34 / SP-10: electric_level == batt_low_stop -> 0 (uses <=)."""
    sp = compute_setpoint(**_serve_args(power_con=300, electric_level=10, batt_low_stop=10))
    assert sp == 0


def test_compute_battery_protection_above_threshold_unaffected():
    """TST-35 / SP-10: electric_level > batt_low_stop -> protection does not fire."""
    sp = compute_setpoint(**_serve_args(power_con=300, electric_level=11, batt_low_stop=10))
    # Same as TST-27 base case: 270
    assert sp == 270


def test_compute_no_battery_latch():
    """TST-36 / SP-10: no latch — recovering SoC immediately allows non-zero setpoint."""
    sp_low = compute_setpoint(**_serve_args(power_con=300, electric_level=10, batt_low_stop=10))
    assert sp_low == 0
    sp_high = compute_setpoint(**_serve_args(power_con=300, electric_level=20, batt_low_stop=10))
    assert sp_high == 270


# ============================================================================
# refine_active_mode (SM-18) — TST-41..47
# ============================================================================

def test_refine_passes_through_serve():
    """TST-41 / SM-18: scheduled 'serve' is never refined."""
    assert refine_active_mode('serve', 50, 'serve', 20, 30) == 'serve'


def test_refine_passes_through_charge():
    """TST-42 / SM-18: scheduled 'charge' is never refined."""
    assert refine_active_mode('charge', 50, 'charge', 20, 30) == 'charge'


def test_refine_dual_low_battery_to_charge():
    """TST-43 / SM-18: SoC == low_stop_pct -> 'charge' (uses <=)."""
    assert refine_active_mode('dual', 20, 'serve', 20, 30) == 'charge'
    assert refine_active_mode('dual', 19, 'serve', 20, 30) == 'charge'


def test_refine_dual_mid_battery_to_dual_limit_from_non_dual():
    """TST-44 / SM-18: low_stop_pct < SoC < threshold AND old != 'dual' -> 'dual-limit'."""
    assert refine_active_mode('dual', 25, 'serve', 20, 30) == 'dual-limit'
    assert refine_active_mode('dual', 21, 'charge', 20, 30) == 'dual-limit'
    assert refine_active_mode('dual', 29, 'serve', 20, 30) == 'dual-limit'


def test_refine_dual_mid_battery_anti_bounce_from_dual():
    """TST-45 / SM-18: same mid-SoC but old == 'dual' -> stays 'dual' (anti-bounce)."""
    assert refine_active_mode('dual', 25, 'dual', 20, 30) == 'dual'
    assert refine_active_mode('dual', 21, 'dual', 20, 30) == 'dual'


def test_refine_dual_high_battery_to_dual():
    """TST-46 / SM-18: SoC >= threshold -> 'dual' regardless of old_mode."""
    assert refine_active_mode('dual', 30, 'serve', 20, 30) == 'dual'
    assert refine_active_mode('dual', 80, 'serve', 20, 30) == 'dual'
    assert refine_active_mode('dual', 30, 'dual', 20, 30) == 'dual'


def test_refine_dual_at_threshold_uses_strict_less():
    """TST-47 / SM-18: SoC == threshold -> 'dual' (strict < to threshold)."""
    assert refine_active_mode('dual', 30, 'serve', 20, 30) == 'dual'


# ============================================================================
# compute_setpoint dual-limit (SP-14) — TST-48..50
# ============================================================================

def test_compute_dual_limit_caps_at_solar_input():
    """TST-48 / SP-14: dual-limit clamps to quantized solar_input_power, no margin."""
    # power_con=500, target=485, quantized=480
    # solar=300 -> solar_cap=(300//30)*30=300; min(480, 300) = 300
    sp = compute_setpoint(**_serve_args(mode='dual-limit', power_con=500, solar_input_power=300))
    assert sp == 300


def test_compute_dual_limit_target_below_solar_cap():
    """TST-49 / SP-14: when target < solar_cap, target wins."""
    # power_con=200, target=185, quantized=180; solar=300, cap=300; min=180
    sp = compute_setpoint(**_serve_args(mode='dual-limit', power_con=200, solar_input_power=300))
    assert sp == 180


def test_compute_dual_limit_zero_solar_zero_setpoint():
    """TST-50 / SP-14: solar=0 -> cap=0 -> setpoint=0 even with positive target."""
    sp = compute_setpoint(**_serve_args(mode='dual-limit', power_con=500, solar_input_power=0))
    assert sp == 0


# ============================================================================
# pick_mode_payload dual-limit (SM-19) — TST-53..54
# ============================================================================

def test_pick_payload_serve_to_dual_limit_no_payload():
    """TST-53 / SM-19: any -> dual-limit emits no MQTT payload (refine_active_mode
    already validated SoC; setpoint loop applies the cap)."""
    payload, mode = pick_mode_payload(
        old_mode='serve', new_mode='dual-limit', bypass_now=False,
        electric_level=25, days_since_last_bypass=3, low_minsoc=100, med_minsoc=200,
    )
    assert payload is None
    assert mode == 'dual-limit'


def test_pick_payload_dual_limit_same_mode_no_bypass_quiet():
    """TST-54 / SM-19: dual-limit -> dual-limit with no bypass -> (None, dual-limit)."""
    payload, mode = pick_mode_payload(
        old_mode='dual-limit', new_mode='dual-limit', bypass_now=False,
        electric_level=25, days_since_last_bypass=3, low_minsoc=100, med_minsoc=200,
    )
    assert payload is None
    assert mode == 'dual-limit'


# ============================================================================
# force_weekly_charge (SM-20) — TST-55..58
# ============================================================================

def test_force_weekly_charge_below_threshold_passes_through():
    """TST-55 / SM-20: hours_since < threshold -> mode unchanged."""
    assert force_weekly_charge('serve', 100, 174) == 'serve'
    assert force_weekly_charge('dual', 173.9, 174) == 'dual'
    assert force_weekly_charge('dual-limit', 0, 174) == 'dual-limit'


def test_force_weekly_charge_at_threshold_forces_charge():
    """TST-56 / SM-20: hours_since == threshold -> 'charge' (uses >=)."""
    assert force_weekly_charge('serve', 174, 174) == 'charge'
    assert force_weekly_charge('dual', 174, 174) == 'charge'


def test_force_weekly_charge_well_above_threshold():
    """TST-57 / SM-20: any mode -> 'charge' when hours far exceed threshold."""
    assert force_weekly_charge('serve', 999, 174) == 'charge'
    assert force_weekly_charge('dual-limit', 500, 174) == 'charge'


def test_force_weekly_charge_already_charge_stays_charge():
    """TST-58 / SM-20: charge stays charge, threshold doesn't matter."""
    assert force_weekly_charge('charge', 50, 174) == 'charge'
    assert force_weekly_charge('charge', 200, 174) == 'charge'


# ============================================================================
# battery_discharged_latch (SP-16) — TST-59..63
# ============================================================================

def test_latch_off_high_level_stays_off():
    """TST-59 / SP-16: latch False, level well above floor -> stays False."""
    assert battery_discharged_latch(50, 10, 5, prev_latched=False) is False


def test_latch_off_drops_to_floor_engages():
    """TST-60 / SP-16: latch False, level <= floor -> latch engages."""
    assert battery_discharged_latch(10, 10, 5, prev_latched=False) is True
    assert battery_discharged_latch(9, 10, 5, prev_latched=False) is True


def test_latch_on_partial_recovery_holds():
    """TST-61 / SP-16: latch True, level recovered but not by hysteresis -> still latched.

    floor=10, hysteresis=5 means release at >=15. Levels 11..14 hold the latch.
    """
    assert battery_discharged_latch(11, 10, 5, prev_latched=True) is True
    assert battery_discharged_latch(14, 10, 5, prev_latched=True) is True


def test_latch_on_full_recovery_releases():
    """TST-62 / SP-16: latch True, level >= floor + hysteresis -> latch releases."""
    assert battery_discharged_latch(15, 10, 5, prev_latched=True) is False
    assert battery_discharged_latch(20, 10, 5, prev_latched=True) is False


def test_latch_no_chatter_at_floor():
    """TST-63 / SP-16: with latch ON, dipping back to floor keeps latched (no flap)."""
    assert battery_discharged_latch(10, 10, 5, prev_latched=True) is True


# ============================================================================
# compute_setpoint with battery_discharged latch (SP-16) — TST-64..65
# ============================================================================

def test_compute_setpoint_latched_forces_zero_above_floor():
    """TST-64 / SP-16: battery_discharged=True forces 0 even if level > batt_low_stop."""
    sp = compute_setpoint(**_serve_args(
        power_con=300, electric_level=12, batt_low_stop=10, battery_discharged=True,
    ))
    assert sp == 0


def test_compute_setpoint_latched_release_resumes_normal():
    """TST-65 / SP-16: once unlatched, normal compute resumes (no residual force-zero)."""
    sp = compute_setpoint(**_serve_args(
        power_con=300, electric_level=20, batt_low_stop=10, battery_discharged=False,
    ))
    assert sp == 270  # same as TST-27 base case


# ============================================================================
# effective_batt_low_stop (SP-18) — TST-66..70
# ============================================================================

def test_effective_low_stop_bypass_now_returns_after():
    """TST-66 / SP-18: bypass_now=True picks the after-bypass floor regardless of hours."""
    assert effective_batt_low_stop(True, 999.0, 10, 20, 10) == 10


def test_effective_low_stop_inside_window_returns_after():
    """TST-67 / SP-18: hours < window picks after-bypass floor."""
    assert effective_batt_low_stop(False, 5.0, 10, 20, 10) == 10


def test_effective_low_stop_at_window_boundary_returns_default():
    """TST-68 / SP-18: hours == window flips to default (strict <)."""
    assert effective_batt_low_stop(False, 10.0, 10, 20, 10) == 20


def test_effective_low_stop_outside_window_returns_default():
    """TST-69 / SP-18: hours > window picks default floor."""
    assert effective_batt_low_stop(False, 24.0, 10, 20, 10) == 20


def test_effective_low_stop_bypass_now_overrides_stale_hours():
    """TST-70 / SP-18: bypass_now wins over a stale hours_since_last_bypass."""
    assert effective_batt_low_stop(True, 9999.0, 10, 20, 10) == 10


# ============================================================================
# derive_hm400_from_shelly (SP-17) — TST-71..73
# ============================================================================

def test_derive_hm400_normal_case():
    """TST-71 / SP-17: shelly > outputhomepower -> positive difference."""
    # 800 W at the house feed with 600 W from Zendure -> ~200 W from HM-400.
    assert derive_hm400_from_shelly(800, 600) == 200


def test_derive_hm400_clamps_negative_to_zero():
    """TST-72 / SP-17: outputhomepower momentarily exceeding shelly clamps at 0.

    Can happen during measurement skew (Shelly @ 1 Hz vs Zendure MQTT lag) or
    when HM-1500 is producing while HM-400 is offline / dark. Returning a
    negative number would corrupt the setpoint formula.
    """
    assert derive_hm400_from_shelly(500, 600) == 0


def test_derive_hm400_zero_inputs():
    """TST-73 / SP-17: night / both inverters idle -> 0."""
    assert derive_hm400_from_shelly(0, 0) == 0
