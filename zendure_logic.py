"""Pure logic for the Zendure SolarFlow AppDaemon apps.

No AppDaemon imports. Tested with pytest from the repo root.
Behavioural spec is in zendure-requirements.md; each test references the
REQ IDs (BT-, SM-, SP-) it covers.
"""


# Anything outside this set ('unknown' / 'unavailable' / None from HA) is
# treated as "no previous mode" per SM-7 in pick_mode_payload.
KNOWN_MODES = ('serve', 'charge', 'dual', 'dual-limit')


def is_bypass_active(electric_level, packstate, outputpackpower, solarinputpower, solar_threshold):
    """Bypass-tracker predicate. See BT-4."""
    # Strict > on solar: at low irradiance inverter measurement noise hovers
    # near 50 W; an >= check would false-trigger constantly.
    return (electric_level == 100
            and packstate == 'idle'
            and outputpackpower == 0
            and solarinputpower > solar_threshold)


def bypass_status(app_active, zendure_active):
    """Combine our derived bypass predicate with Zendure's reported `pass` flag.

    Returns one of: 'none', 'app_only', 'zendure_only', 'both'. Charting this
    side-by-side surfaces both the case we're working around (app sees bypass
    while Zendure stays silent) and any case where Zendure reports bypass but
    our predicate disagrees (would warrant a predicate review).
    """
    if app_active and zendure_active:
        return "both"
    if app_active:
        return "app_only"
    if zendure_active:
        return "zendure_only"
    return "none"


def pick_operation_mode(hour, schedule):
    """Hour-based mode lookup. See SM-4, SM-5."""
    return schedule[hour]


def force_weekly_charge(mode, hours_since_last_bypass, weekly_charge_hours):
    """Final override: if it's been too long since a confirmed bypass, force 'charge'. See SM-20.

    Production parity: 7.5 d (174 h) without a confirmed bypass forces a
    full charge regardless of hour-of-day or SoC. Ensures the battery
    cycles to full at least weekly even in winter / multi-day overcast.
    """
    if hours_since_last_bypass >= weekly_charge_hours:
        return 'charge'
    return mode


def effective_batt_low_stop(bypass_now, hours_since_last_bypass,
                            after_bypass_pct, default_pct, window_hours):
    """Pick the active discharge floor based on bypass recency. See SP-18.

    Production sets `zendure.batt_low_stop` dynamically: 10 % within ~10 h of
    a bypass moment (or while bypass is live), 20 % otherwise. This is the
    functional, non-sticky equivalent — re-evaluated each tick rather than
    persisted between mode transitions. Trade-off: on the bypass-window
    boundary the floor flips back to 20 % cleanly, where production stays at
    10 % until the next mode transition rewrites it.
    """
    if bypass_now or hours_since_last_bypass < window_hours:
        return after_bypass_pct
    return default_pct


def battery_discharged_latch(electric_level, batt_low_stop, hysteresis_pct, prev_latched):
    """Latch with hysteresis to keep us OFF discharge once the floor is hit. See SP-16.

    - Not latched + level <= floor               -> latch ON
    - Already latched + level >= floor+hysteresis -> latch OFF
    - Otherwise (between)                         -> hold previous

    Production uses a 5 % hysteresis. Without it a 1 % SoC bounce flaps
    discharge on/off; without the latch at all, recovery from low SoC
    re-enables discharge before any meaningful charge has accumulated.
    """
    if not prev_latched:
        return electric_level <= batt_low_stop
    return electric_level < batt_low_stop + hysteresis_pct


def refine_active_mode(scheduled_mode, electric_level, old_mode, low_stop_pct, dual_limit_threshold_pct):
    """Refine 'dual' to charge/dual-limit/dual based on battery state. See SM-18.

    For non-'dual' scheduled entries returns input unchanged. For 'dual' hours
    (the battery-active window), production parity:

      level <= low_stop_pct                                -> 'charge'
      level <  dual_limit_threshold_pct AND old != 'dual'  -> 'dual-limit'
      otherwise                                            -> 'dual'

    The `old_mode != 'dual'` anti-bounce keeps us from downgrading mid-day:
    once we've committed to draining (already in 'dual'), a transient SoC dip
    below 30% should not yank us back to dual-limit only-from-solar mode.
    """
    if scheduled_mode != 'dual':
        return scheduled_mode
    if electric_level <= low_stop_pct:
        return 'charge'
    if electric_level < dual_limit_threshold_pct and old_mode != 'dual':
        return 'dual-limit'
    return 'dual'


def pick_mode_payload(old_mode, new_mode, bypass_now, electric_level,
                      days_since_last_bypass, low_minsoc, med_minsoc):
    """Pick MQTT payload and effective mode for a state-machine tick.

    Returns (payload_or_None, effective_mode_string). See SM-7..SM-15.

    `effective_mode` is what the caller should write to
    `zendure.operation_mode`. Usually `new_mode`, but a transition guard may
    refuse to advance — the caller writes `old_mode` and the next tick retries.
    """
    # SM-7: cold-start case. None / 'unknown' / 'unavailable' from HA on boot
    # -> adopt new_mode silently. The first real transition fires on the next
    # tick once we have a known previous mode to transition FROM.
    if old_mode not in KNOWN_MODES:
        return _no_change_payload(new_mode, bypass_now, low_minsoc)

    # SM-14 / SM-15: same mode. Only emit if bypass needs renewing.
    if old_mode == new_mode:
        return _no_change_payload(new_mode, bypass_now, low_minsoc)

    # Real transition between two known modes.
    if new_mode == 'charge':
        # SM-13: charge is always allowed; the schedule decided.
        payload = {'properties': {'outputLimit': 0, 'passMode': 0, 'minSoc': low_minsoc}}
        return payload, 'charge'

    if new_mode == 'serve':
        if bypass_now:
            # SM-8: passMode=1 keeps solar passthrough open as we exit charge.
            payload = {'properties': {'outputLimit': 0, 'passMode': 1, 'minSoc': low_minsoc}}
            return payload, 'serve'
        if electric_level >= 30 and days_since_last_bypass < 7:
            # SM-9: healthy SoC + recent bypass -> advance with the medium
            # discharge floor. Recent bypass means the battery cycled to full
            # lately, so a slightly more aggressive floor is safe.
            payload = {'properties': {'outputLimit': 0, 'minSoc': med_minsoc}}
            return payload, 'serve'
        # SM-10: neither guard cleared. Stay put; next tick re-evaluates.
        return None, old_mode

    if new_mode == 'dual':
        # SM-11: delay only when BOTH SoC is low AND bypass is recent.
        # Stale bypass implies winter — advance anyway (SM-12) to avoid
        # stranding in charge forever waiting for a bypass that may never come.
        if electric_level < 20 and days_since_last_bypass < 7:
            return None, old_mode
        # SM-12: dual itself emits no payload; setpoint loop handles caps.
        return None, 'dual'

    if new_mode == 'dual-limit':
        # SM-19: dual-limit emits no payload — refine_active_mode already
        # validated SoC against low_stop_pct, and the setpoint loop applies
        # the solar-input cap. No transition guard needed.
        return None, 'dual-limit'

    # Defensive: unknown new_mode shouldn't reach here (apps.yaml validates it).
    return None, old_mode


def _no_change_payload(current_mode, bypass_now, low_minsoc):
    """SM-7 / SM-14 / SM-15 helper.

    If we're in bypass right now we still publish a passthrough payload —
    the inverter would otherwise sit at its previous outputLimit and leak
    battery while panels provide free pass-through.
    """
    if bypass_now:
        payload = {'properties': {'outputLimit': 0, 'passMode': 0, 'minSoc': low_minsoc}}
        return payload, current_mode
    return None, current_mode


def derive_bypass_now(outputpackpower, packstate):
    """Instantaneous bypass guess for ZendureSetpoint (every 20 s). See SP-4.

    Coarser than is_bypass_active (no SoC / solar / debounce); the setpoint
    loop only needs "is the battery currently passing solar straight through?".
    """
    return outputpackpower == 0 and packstate == 'idle'


def compute_setpoint(power_con, power_sol, mode, solar_input_power, electric_level,
                     batt_low_stop, inverter_max_power, dual_max_power, dual_solar_margin,
                     power_step, target_bias_steps,
                     bypass_now=False, hours_since_last_bypass=999, bypass_grace_hours=4,
                     battery_discharged=False):
    """Compute the inverter outputLimit setpoint. See SP-5..SP-11, SP-14..SP-15.

    Pipeline: raw target -> quantize -> mode cap -> bypass-grace override
    -> battery guard -> clamp.

    The half-step bias (target_bias_steps = 0.5) shaves half a step off the
    raw target before flooring. Without it the quantizer consistently
    OVER-produces by up to power_step W while consumption hovers just below
    a step boundary, causing visible export to the grid. Half-step bias
    shifts the rounding point to the midpoint, so on average we
    under-produce by half a step instead of over-producing by a full one.
    """
    # SP-7: charge forces 0 regardless of all other inputs.
    if mode == 'charge':
        return 0

    # SP-5 + SP-6: bias and quantize.
    raw_target = power_con - power_sol - (power_step * target_bias_steps)
    quantized_target = (raw_target // power_step) * power_step

    if mode == 'dual':
        # SP-8: dual feeds the home from solar AND battery in parallel but
        # capped at the panels' contribution, so we never drain the battery
        # past what the sun is providing — that would defeat 'dual'.
        half_solar = ((solar_input_power - dual_solar_margin) // power_step) * power_step
        if half_solar < 0:
            half_solar = 0
        cap = dual_max_power
        setpoint = min(quantized_target, half_solar, cap)
    elif mode == 'dual-limit':
        # SP-14: cap at the inverter's current solar input, quantized. Net
        # effect: output exactly matches solar production, so the battery
        # never drains. Used in the SoC band between low_stop and
        # dual_limit_threshold to keep topping up on bad-weather days.
        solar_cap = (solar_input_power // power_step) * power_step
        if solar_cap < 0:
            solar_cap = 0
        cap = solar_cap
        setpoint = min(quantized_target, cap)
        # SP-15: bypass-grace override. If we're currently in bypass OR a
        # bypass landed within the last `bypass_grace_hours`, the battery is
        # known full enough to drain freely — lift the cap to dual_max_power
        # so we actually use that fresh charge instead of hoarding it.
        if bypass_now or hours_since_last_bypass < bypass_grace_hours:
            cap = dual_max_power
            setpoint = min(quantized_target, cap)
    else:
        cap = inverter_max_power
        setpoint = quantized_target

    # SP-10 / SP-16: battery protection. The simple `level <= batt_low_stop`
    # check still fires; the optional `battery_discharged` latch (computed by
    # the caller via battery_discharged_latch()) extends the lockout above
    # batt_low_stop until level recovers by hysteresis_pct.
    if electric_level <= batt_low_stop or battery_discharged:
        setpoint = 0

    # SP-11: clamp to [0, cap].
    if setpoint < 0:
        setpoint = 0
    if setpoint > cap:
        setpoint = cap

    return int(setpoint)
