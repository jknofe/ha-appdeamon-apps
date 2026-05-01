"""Pure logic for the Zendure SolarFlow AppDaemon apps.

No AppDaemon imports. Tested with pytest from the repo root.
Behavioural spec is in zendure-requirements.md; each test references the
REQ IDs (BT-, SM-, SP-) it covers.
"""


# Anything outside this set ('unknown' / 'unavailable' / None from HA) is
# treated as "no previous mode" per SM-7 in pick_mode_payload.
KNOWN_MODES = ('serve', 'charge', 'dual')


def is_bypass_active(electric_level, packstate, outputpackpower, solarinputpower, solar_threshold):
    """Bypass-tracker predicate. See BT-4."""
    # Strict > on solar: at low irradiance inverter measurement noise hovers
    # near 50 W; an >= check would false-trigger constantly.
    return (electric_level == 100
            and packstate == 'idle'
            and outputpackpower == 0
            and solarinputpower > solar_threshold)


def pick_operation_mode(hour, schedule):
    """Hour-based mode lookup. See SM-4, SM-5."""
    return schedule[hour]


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
                     power_step, target_bias_steps):
    """Compute the inverter outputLimit setpoint. See SP-5..SP-11.

    Pipeline: raw target -> quantize -> mode cap -> battery guard -> clamp.

    The half-step bias (target_bias_steps = 0.5) shaves half a step off the
    raw target before flooring. Without it the quantizer consistently
    OVER-produces by up to power_step W while consumption hovers just below
    a step boundary, causing visible export to the grid. Half-step bias
    shifts the rounding point to the midpoint, so on average we
    under-produce by half a step instead of over-producing by a full one.
    Recent user tuning, port verbatim.
    """
    # SP-7: charge forces 0 regardless of all other inputs.
    if mode == 'charge':
        return 0

    # SP-5 + SP-6: bias and quantize. Floor-division on a float yields a
    # float, hence the int() at return.
    raw_target = power_con - power_sol - (power_step * target_bias_steps)
    quantized_target = (raw_target // power_step) * power_step

    if mode == 'dual':
        # SP-8: dual feeds the home from solar AND battery in parallel but
        # capped at the panels' contribution, so we never drain the battery
        # past what the sun is providing — that would defeat 'dual'.
        half_solar = ((solar_input_power - dual_solar_margin) // power_step) * power_step
        if half_solar < 0:
            # Cloudy / dawn / dusk — clamp before it caps the setpoint.
            half_solar = 0
        cap = dual_max_power
        setpoint = min(quantized_target, half_solar, cap)
    else:
        cap = inverter_max_power
        setpoint = quantized_target

    # SP-10: battery protection. No latch, no hysteresis (TST-36 pins this).
    # The previous design latched a `battery_discharged` flag the user had to
    # clear by hand; a 1 % SoC bounce ping-ponging discharge on/off turned
    # out to be less harmful than the stuck latch.
    if electric_level <= batt_low_stop:
        setpoint = 0

    # SP-11: clamp to [0, cap]. Negative target = we're already net-exporting;
    # the inverter idles rather than pushing power backwards.
    if setpoint < 0:
        setpoint = 0
    if setpoint > cap:
        setpoint = cap

    return int(setpoint)
