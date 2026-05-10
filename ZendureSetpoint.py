"""Zendure SolarFlow controller — picks mode + computes outputLimit (every 20 s).

Lean rewrite. Goal: maximize self-consumption of solar — only what the home
cannot use right now is stored in the battery.

The control law is one equation:

    outputLimit = max(0, power_consumption - solar_secondary_power)

solar_secondary_power is the uncontrolled solar inverter (HM-400). The
Zendure-fed inverter (solar_primary, HM-1500) is what we drive via
outputLimit; it sources from solar_input_power (Zendure-side panels)
first and the battery as needed. See README.md for the energy-flow
diagram and apps.yaml for the configurable sensor names.

Everything else in this file is the protective scaffolding around it:
  - SoC floor (10 % within 10 h of bypass, 20 % otherwise) so we don't drain to 0
  - Charge latch with 5 % hysteresis so a 1 % SoC bounce doesn't flap discharge
  - Three modes that pick a cap on outputLimit:
      'charge'      → 0           (battery only charges; solar to home via HM-1500 stops)
      'solar-only'  → solar_input (mid-SoC daylight; battery preserved, surplus charges)
      'free'        → max_cap     (battery drains as needed; surplus solar still charges)
  - Weekly force-charge so the battery hits 100 % at least every 7.5 days

The state-machine app is now bypass-tracker-only; mode lives here so the
20-s tick re-derives it from current state without a separate cadence.
"""
import datetime
import json

import appdaemon.plugins.hass.hassapi as hass

from app_helpers import parse_interval


MODE_CHARGE     = 'charge'
MODE_SOLAR_ONLY = 'solar-only'
MODE_FREE       = 'free'


# ----------------------------------------------------------------------
# Pure functions — no AppDaemon, no I/O. Testable in isolation.
# ----------------------------------------------------------------------

def effective_floor(hours_since_bypass, after_bypass_pct, default_pct, window_hours):
    """SoC floor below which we stop discharging. Drops to `after_bypass_pct`
    inside the post-bypass window so we can use just-charged energy more
    deeply, then back to `default_pct` outside it."""
    if hours_since_bypass < window_hours:
        return after_bypass_pct
    return default_pct


def update_charge_latch(soc, floor, hysteresis_pct, was_latched):
    """Latch with hysteresis on the charge trigger. Engages at SoC ≤ floor,
    releases at SoC ≥ floor + hysteresis. Without it a 1 % SoC bounce flaps
    discharge on/off at the boundary."""
    if was_latched:
        return soc < floor + hysteresis_pct
    return soc <= floor


def pick_mode(soc, solar_input, hours_since_bypass,
              charge_latched, free_latched_in,
              soc_promote, solar_threshold, weekly_force_hours):
    """Returns (mode, free_latched_out).

    Decision order:
      1. weekly force-charge (battery health) → 'charge'
      2. charge latch engaged                → 'charge'
      3. SoC has reached promote threshold OR free_latch already on → 'free'
      4. real daylight + mid-SoC → 'solar-only' (conserve battery, charge from sun)
      5. otherwise → 'free' (mid-SoC at night: battery is the only buffer)

    The free_latch is the daily drain commitment: once SoC has reached
    soc_promote at least once, we stay in 'free' until charge mode resets it.
    Prevents a transient mid-day SoC dip from yanking us back to solar-only
    and stranding stored energy.
    """
    if hours_since_bypass >= weekly_force_hours:
        return (MODE_CHARGE, False)
    if charge_latched:
        return (MODE_CHARGE, False)
    free_latched_out = free_latched_in or (soc >= soc_promote)
    if free_latched_out:
        return (MODE_FREE, True)
    if solar_input > solar_threshold:
        return (MODE_SOLAR_ONLY, False)
    return (MODE_FREE, False)


def compute_setpoint(consumption, solar_secondary, solar_input, mode,
                     max_cap, power_step, bias_steps):
    """Pipeline: target → quantize → mode cap → clamp.

    The control law: outputLimit = max(0, consumption - solar_secondary).
    solar_secondary is the uncontrolled solar inverter (HM-400 in this
    install) — what's already going to home from outside our control.
    Subtracting it leaves the deficit we ask the Zendure-fed inverter
    (solar_primary, HM-1500) to cover, sourcing from solar_input first
    and battery as needed.

    Half-step bias shifts the floor-quantize result down by half a step
    so we err on slight under-supply (small grid import) instead of
    slight over-supply (small grid export).
    """
    if mode == MODE_CHARGE:
        return 0
    raw_target = consumption - solar_secondary - (power_step * bias_steps)
    quantized = (raw_target // power_step) * power_step
    if mode == MODE_SOLAR_ONLY:
        cap = (solar_input // power_step) * power_step
        if cap < 0:
            cap = 0
    else:
        cap = max_cap
    setpoint = min(quantized, cap)
    if setpoint < 0:
        setpoint = 0
    if setpoint > cap:
        setpoint = cap
    return int(setpoint)


# ----------------------------------------------------------------------
# AppDaemon glue
# ----------------------------------------------------------------------

class ZendureSetpoint(hass.Hass):

    POST_BYPASS_WINDOW_HOURS = 10
    LATCH_HYSTERESIS_PCT = 5

    def initialize(self):
        a = self.args
        self.update_interval        = parse_interval(a.get("update_interval", "20s"))
        self.mqtt_topic_write       = a["mqtt_topic_write"]
        self.max_cap                = a.get("max_cap", 720)
        self.power_step             = a.get("power_step", 30)
        self.bias_steps             = a.get("power_target_bias_steps", 0.5)
        self.floor_after_bypass     = a.get("batt_floor_after_bypass", 10)
        self.floor_default          = a.get("batt_floor_default", 20)
        self.soc_promote            = a.get("soc_promote_to_free", 30)
        self.solar_threshold_w      = a.get("solar_threshold_w", 100)
        self.weekly_force_hours     = a.get("weekly_charge_force_hours", 174)
        # dry_run is set in apps.yaml only — deliberately not a HA helper so
        # it can't be flipped by accident from the dashboard. Default True
        # (shadow) so a missing key is never a surprise live-write.
        self.dry_run                = bool(a.get("dry_run", True))
        # Configurable external power-input sensors. See apps.yaml comment
        # and README.md for the physical mapping. Defaults preserve the
        # legacy hardcoded entity IDs for this user's installation.
        pi = a.get("power_inputs", {})
        self.power_consumption_sensor      = pi.get("power_consumption",     "sensor.power_consumption")
        self.solar_primary_power_sensor    = pi.get("solar_primary_power",   "sensor.zendure_mqtt_outputhomepower")
        self.solar_secondary_power_sensor  = pi.get("solar_secondary_power", "sensor.hm_400_power")
        self.solar_input_power_sensor      = pi.get("solar_input_power",     "sensor.zendure_mqtt_solarinputpower")

        # In-memory state. charge_latch is bootstrapped from HA so a restart
        # mid-discharge doesn't briefly re-enable drain. free_latch is
        # always re-derived from the next tick (one tick of slack is fine).
        self._charge_latch = self._bootstrap_charge_latch()
        self._free_latch = False
        self._setpoint_old = self._get_state_int("sensor.zendure_setpoint", default=None)
        self._mode_old = None
        self._is_running = False

        self.run_every(self._tick, "now", self.update_interval)
        self.log("ZendureSetpoint started")

    def _tick(self, kwargs):
        if self._is_running:
            return
        try:
            self._is_running = True

            soc                 = self._get_state_int("sensor.zendure_mqtt_electriclevel")
            consumption         = self._get_state_int(self.power_consumption_sensor)
            solar_secondary     = self._get_state_int(self.solar_secondary_power_sensor)
            solar_input         = self._get_state_int(self.solar_input_power_sensor)
            hours_since_bypass  = self._hours_since_last_bypass()

            floor = effective_floor(
                hours_since_bypass,
                self.floor_after_bypass, self.floor_default,
                self.POST_BYPASS_WINDOW_HOURS,
            )

            new_charge_latch = update_charge_latch(
                soc, floor, self.LATCH_HYSTERESIS_PCT, self._charge_latch,
            )
            if new_charge_latch != self._charge_latch:
                self._charge_latch = new_charge_latch
                self._write_battery_discharged_sensor(new_charge_latch)
                if new_charge_latch:
                    # Hitting the floor resets the daily drain commitment.
                    self._free_latch = False

            mode, self._free_latch = pick_mode(
                soc, solar_input, hours_since_bypass,
                self._charge_latch, self._free_latch,
                self.soc_promote, self.solar_threshold_w, self.weekly_force_hours,
            )

            setpoint = compute_setpoint(
                consumption, solar_secondary, solar_input, mode,
                self.max_cap, self.power_step, self.bias_steps,
            )

            self._write_setpoint(setpoint)
            self._write_mode(mode)
            if mode != self._mode_old:
                if self._mode_old is not None:
                    self.log(f"Mode {self._mode_old} -> {mode}")
                self._mode_old = mode
            if self._setpoint_old != setpoint:
                self._publish_outputlimit(setpoint)
                self._setpoint_old = setpoint
        except Exception as e:
            self.log(f"Error in _tick: {e}", level="ERROR")
        finally:
            self._is_running = False

    # ------------------------------------------------------------------
    # HA writes (shadow-aware)
    # ------------------------------------------------------------------

    def _write_setpoint(self, setpoint):
        # State string formatted as repr(round(x, 0)) to match the legacy
        # python_script byte-for-byte (e.g. "30.0") so shadow vs live charts
        # compare cleanly during the verification window.
        state_str = repr(round(setpoint, 0))
        if self.dry_run:
            target, friendly = "sensor.zendure_setpoint_shadow", "Zendure Setpoint (shadow)"
        else:
            target, friendly = "sensor.zendure_setpoint", "Zendure Setpoint"
        if self.get_state(target) == state_str:
            return
        self.set_state(target, state=state_str, attributes={
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "device_class": "power",
            "friendly_name": friendly,
        })

    def _write_mode(self, mode):
        if self.dry_run:
            target, friendly = "sensor.zendure_operation_mode_shadow", "Zendure Operation Mode (shadow)"
        else:
            target, friendly = "zendure.operation_mode", "Zendure Operation Mode"
        if self.get_state(target) == mode:
            return
        self.set_state(target, state=mode, attributes={"friendly_name": friendly})

    def _write_battery_discharged_sensor(self, latched):
        state_str = "True" if latched else "False"
        if self.dry_run:
            self.set_state("sensor.zendure_battery_discharged_shadow",
                           state=state_str,
                           attributes={"friendly_name": "Zendure Battery Discharged (shadow)"})
        else:
            self.set_state("sensor.zendure_battery_discharged",
                           state=state_str,
                           attributes={"friendly_name": "Zendure Battery Discharged"})

    def _publish_outputlimit(self, setpoint):
        payload = json.dumps({"properties": {"outputLimit": setpoint}})
        topic = f"shadow/{self.mqtt_topic_write}" if self.dry_run else self.mqtt_topic_write
        try:
            self.call_service("mqtt/publish", topic=topic, payload=payload)
        except Exception as e:
            self.log(f"MQTT publish failed: {e}", level="ERROR")

    # ------------------------------------------------------------------
    # HA reads
    # ------------------------------------------------------------------

    def _get_state_int(self, entity_id, default=0):
        val = self.get_state(entity_id)
        if val in (None, "unknown", "unavailable"):
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    def _hours_since_last_bypass(self):
        """Hours since sensor.zendure_bypass_reached_at. On any error, returns
        a large value — safe direction (post-bypass deep-drain stays disengaged
        and the weekly force-charge fires; we'd rather charge than over-drain)."""
        raw = self.get_state("sensor.zendure_bypass_reached_at")
        if raw in (None, "unknown", "unavailable"):
            return 999.0
        try:
            last = datetime.datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return 999.0
        now = self.datetime()
        if (last.tzinfo is None) != (now.tzinfo is None):
            last = last.replace(tzinfo=now.tzinfo)
        return (now - last).total_seconds() / 3600.0

    def _bootstrap_charge_latch(self):
        for entity in ("sensor.zendure_battery_discharged_shadow",
                       "zendure.battery_discharged"):
            v = self.get_state(entity)
            if v in (None, "unknown", "unavailable"):
                continue
            return str(v).lower() in ("true", "on")
        return False
