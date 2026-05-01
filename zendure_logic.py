"""Pure logic for the Zendure SolarFlow AppDaemon apps.

No AppDaemon imports. Functions here are exercised by pytest from the repo
root (see tests/). Behavioural spec is in zendure-requirements.md; each test
references the REQ IDs (BT-, SM-, SP-) it covers.

Stubs raise NotImplementedError until the matching task in zendure-tasks.md
Phase 3a / 4a is implemented.
"""


def is_bypass_active(electric_level, packstate, outputpackpower, solarinputpower, solar_threshold):
    """Bypass-tracker predicate. See BT-4."""
    raise NotImplementedError


def pick_operation_mode(hour, schedule):
    """Hour-based mode lookup. See SM-4, SM-5."""
    raise NotImplementedError


def pick_mode_payload(old_mode, new_mode, bypass_now, electric_level,
                      days_since_last_bypass, low_minsoc, med_minsoc):
    """Pick MQTT payload and effective mode for a state-machine tick.

    Returns (payload_dict_or_None, effective_mode_string).
    See SM-7 through SM-15.
    """
    raise NotImplementedError


def derive_bypass_now(outputpackpower, packstate):
    """Instantaneous bypass derivation used by ZendureSetpoint. See SP-4."""
    raise NotImplementedError


def compute_setpoint(power_con, power_sol, mode, solar_input_power, electric_level,
                     batt_low_stop, inverter_max_power, dual_max_power, dual_solar_margin,
                     power_step, target_bias_steps):
    """Compute the inverter outputLimit setpoint. See SP-5 through SP-11."""
    raise NotImplementedError
