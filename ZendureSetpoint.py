"""Zendure SolarFlow output-setpoint controller (every 20 s).

Replaces python_script.zendure_setpoint. Reads consumption / solar / battery
state, calls compute_setpoint, publishes MQTT only on change.

Pure decision logic lives in zendure_logic.py; this file is the AppDaemon
glue: read HA state, call the pure function, write HA state, publish MQTT.

See zendure-requirements.md section 4 for the contract.
"""
import datetime
import json

import appdaemon.plugins.hass.hassapi as hass

from app_helpers import parse_interval
from zendure_logic import (
    battery_discharged_latch,
    compute_setpoint,
    derive_bypass_now,
    effective_batt_low_stop,
)


class ZendureSetpoint(hass.Hass):

    def initialize(self):
        # Config from apps.yaml (see knowledgebase / requirements §7)
        self.update_interval = parse_interval(self.args.get("update_interval", "20s"))
        self.mqtt_topic_write = self.args["mqtt_topic_write"]
        # Two caps: dual_cap (battery drains freely up to this), serve_cap
        # (lower so a sudden consumption drop between 20 s ticks bounds export).
        # dual-limit caps at quantized solar input separately inside compute_setpoint.
        self.dual_cap = self.args.get("dual_cap", 720)
        self.serve_cap = self.args.get("serve_cap", 540)
        self.power_step = self.args.get("power_step", 30)
        # SP-18: production parity — discharge floor is dynamic. After a bypass
        # moment (or while bypass is live), allow draining to 10 %; outside that
        # window, hold 20 % so the battery keeps a reserve until next charge.
        self.batt_low_stop_after_bypass = self.args.get("batt_low_stop_after_bypass", 10)
        self.batt_low_stop_default = self.args.get("batt_low_stop_default", 20)
        self.low_stop_after_bypass_hours = self.args.get("low_stop_after_bypass_hours", 10)
        self.power_target_bias_steps = self.args.get("power_target_bias_steps", 0.5)
        # SP-16: battery-discharged latch hysteresis. Once level <= batt_low_stop
        # the latch sticks until level >= batt_low_stop + hysteresis, so a 1%
        # SoC bounce can't ping-pong discharge on/off.
        self.batt_low_stop_hysteresis_pct = self.args.get("batt_low_stop_hysteresis_pct", 5)
        # In-memory latch; bootstrap from HA so we don't drop a latched
        # state across an AppDaemon restart.
        self._battery_discharged = self._bootstrap_battery_discharged()

        self._is_running = False

        # PS-2: bootstrap setpoint_old from the live HA value once, so the
        # first cycle's change-detect knows what we last published. None on
        # cold-start forces a publish on the first computed setpoint.
        self._setpoint_old = self._get_state_int("sensor.zendure_setpoint", default=None)

        # First tick fires at start + update_interval (20 s by default). No
        # kickoff: 20 s is short enough not to matter, and letting the state
        # machine's own kickoff write zendure.operation_mode first means our
        # first setpoint tick reads a fresh mode rather than a cold-start
        # 'serve' default.
        self.run_every(self._tick, "now", self.update_interval)
        self.log("ZendureSetpoint started")

    def _tick(self, kwargs):
        # CC-5: in-flight reentry guard, mirroring PowerMeter.py.
        if self._is_running:
            self.log("Tick already running, skipping")
            return
        try:
            self._is_running = True

            # Read inputs. CC-6: missing / unknown values fall through to defaults.
            mode = self.get_state("zendure.operation_mode")
            if mode in (None, "unknown", "unavailable"):
                mode = "serve"

            power_con = self._get_state_int("sensor.power_consumption")
            power_sol = self._read_power_sol()
            solar_input_power = self._get_state_int("sensor.zendure_mqtt_solarinputpower")
            electric_level = self._get_state_int("sensor.zendure_mqtt_electriclevel")
            outputpackpower = self._get_state_int("sensor.zendure_mqtt_outputpackpower")
            packstate = self.get_state("sensor.zendure_mqtt_packstate") or ""
            bypass_now = derive_bypass_now(outputpackpower, packstate)
            hours_since_last_bypass = self._hours_since_last_bypass()

            # SP-18: pick the active floor (10 % inside the post-bypass window,
            # 20 % outside). Used by both the latch and compute_setpoint so the
            # cutoff and the latch hysteresis stay aligned.
            batt_low_stop = effective_batt_low_stop(
                bypass_now, hours_since_last_bypass,
                self.batt_low_stop_after_bypass, self.batt_low_stop_default,
                self.low_stop_after_bypass_hours,
            )

            # SP-16: update the discharge latch BEFORE computing setpoint, so a
            # fresh transition takes effect this tick. set_state only when the
            # bool flips to keep HA history clean.
            new_latched = battery_discharged_latch(
                electric_level, batt_low_stop,
                self.batt_low_stop_hysteresis_pct, self._battery_discharged,
            )
            if new_latched != self._battery_discharged:
                self._battery_discharged = new_latched
                self._write_battery_discharged_sensor(new_latched)

            setpoint = compute_setpoint(
                power_con=power_con,
                power_sol=power_sol,
                mode=mode,
                solar_input_power=solar_input_power,
                electric_level=electric_level,
                batt_low_stop=batt_low_stop,
                dual_cap=self.dual_cap,
                serve_cap=self.serve_cap,
                power_step=self.power_step,
                target_bias_steps=self.power_target_bias_steps,
                battery_discharged=self._battery_discharged,
            )

            self._write_setpoint(setpoint)
            # SP-13: publish MQTT only on a real change.
            if self._setpoint_old != setpoint:
                self._publish_setpoint_mqtt(setpoint)
                self._setpoint_old = setpoint
        except Exception as e:
            self.log(f"Error in _tick: {e}", level="ERROR")
        finally:
            self._is_running = False

    def _write_setpoint(self, setpoint):
        """Shadow write under dry_run, otherwise the live sensor.

        State string formatted as `repr(round(setpoint, 0))` to match the
        original python_script byte-for-byte (e.g. "30.0"), so shadow vs
        live charts compare cleanly during the verification window.
        """
        state_str = repr(round(setpoint, 0))
        attrs = {
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "device_class": "power",
        }
        if self._dry_run():
            attrs["friendly_name"] = "Zendure Setpoint (shadow)"
            self.set_state("sensor.zendure_setpoint_shadow", state=state_str, attributes=attrs)
        else:
            attrs["friendly_name"] = "Zendure Setpoint"
            self.set_state("sensor.zendure_setpoint", state=state_str, attributes=attrs)

    def _publish_setpoint_mqtt(self, setpoint):
        payload = {"properties": {"outputLimit": setpoint}}
        payload_str = json.dumps(payload)
        # In dry_run, redirect to a shadow-prefixed topic with the same payload
        # so an external subscriber can diff our proposed setpoints against the
        # live python_script's writes on the real topic.
        topic = f"shadow/{self.mqtt_topic_write}" if self._dry_run() else self.mqtt_topic_write
        try:
            self.call_service(
                "mqtt/publish", topic=topic, payload=payload_str
            )
        except Exception as e:
            self.log(f"MQTT publish failed: {e}", level="ERROR")

    # ------------------------------------------------------------------
    # Helpers (mirror ZendureStateMachine; kept inline rather than factored
    # to keep each app file self-contained and match PowerMeter.py style)
    # ------------------------------------------------------------------

    def _get_state_int(self, entity_id, default=0):
        val = self.get_state(entity_id)
        if val in (None, "unknown", "unavailable"):
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    def _read_power_sol(self):
        """SP-17: prefer sensor.hm_400_power; fall back to sensor.hm_400_power_fallback
        when the primary is unavailable. Matches the production script's behaviour
        when the inverter's WiFi drops."""
        primary = self.get_state("sensor.hm_400_power")
        if primary not in (None, "unknown", "unavailable"):
            try:
                return int(float(primary))
            except (ValueError, TypeError):
                pass
        return self._get_state_int("sensor.hm_400_power_fallback")

    def _bootstrap_battery_discharged(self):
        """SP-16: restore latch state from HA across AppDaemon restarts.

        We accept either our shadow sensor or the legacy `zendure.battery_discharged`
        entity (string 'True'/'False' per the production script's format). Default
        to False if neither exists.
        """
        for entity in ("sensor.zendure_battery_discharged_shadow",
                       "zendure.battery_discharged"):
            state = self.get_state(entity)
            if state in (None, "unknown", "unavailable"):
                continue
            return str(state).lower() in ("true", "on")
        return False

    def _write_battery_discharged_sensor(self, latched):
        state_str = "True" if latched else "False"
        attrs = {"friendly_name": "Zendure Battery Discharged (shadow)"}
        if self._dry_run():
            self.set_state("sensor.zendure_battery_discharged_shadow",
                           state=state_str, attributes=attrs)
        else:
            attrs["friendly_name"] = "Zendure Battery Discharged"
            self.set_state("sensor.zendure_battery_discharged",
                           state=state_str, attributes=attrs)

    def _hours_since_last_bypass(self):
        """Hours since the last confirmed bypass, read from sensor.zendure_bypass_reached_at.

        That sensor is written by ZendureStateMachine (live timestamp on real
        bypass, fallback ~7 days ago on cold start). On any read error we
        return a safely-large value so the bypass-grace override doesn't fire
        accidentally.
        """
        raw = self.get_state("sensor.zendure_bypass_reached_at")
        if raw in (None, "unknown", "unavailable"):
            return 999.0
        try:
            last = datetime.datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return 999.0
        now = self.datetime()
        # AppDaemon's self.datetime() may be naive depending on host config;
        # match the saved timestamp's awareness so the subtraction works.
        if (last.tzinfo is None) != (now.tzinfo is None):
            last = last.replace(tzinfo=now.tzinfo)
        return (now - last).total_seconds() / 3600.0

    def _dry_run(self):
        """Default to True (safe / shadow) when the helper is missing."""
        state = self.get_state("input_boolean.zendure_dry_run")
        if state in (None, "unknown", "unavailable"):
            return True
        return state == "on"
