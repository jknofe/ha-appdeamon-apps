"""Zendure SolarFlow operation-mode state machine + bypass tracker.

Replaces python_script.zendure_state_machine and the
automation.zendure_bypass_reached + sensor.zendure_mqtt_bypass workaround.

Pure decision logic lives in zendure_logic.py; this file is the AppDaemon
glue: read HA state, call the pure functions, write HA state, publish MQTT.

See zendure-requirements.md sections 5 and 6 for the contract.
"""
import datetime
import json

import appdaemon.plugins.hass.hassapi as hass

from app_helpers import next_aligned_minute, parse_interval
from zendure_logic import (
    bypass_status,
    is_bypass_active,
    pick_mode_payload,
    pick_operation_mode,
    refine_active_mode,
)


class ZendureStateMachine(hass.Hass):

    BYPASS_INPUT_SENSORS = (
        "sensor.zendure_mqtt_electriclevel",
        "sensor.zendure_mqtt_packstate",
        "sensor.zendure_mqtt_outputpackpower",
        "sensor.zendure_mqtt_solarinputpower",
    )
    BYPASS_ZENDURE_REPORTED_SENSOR = "sensor.zendure_mqtt_bypass"

    def initialize(self):
        # Config from apps.yaml (see knowledgebase / requirements §7)
        self.update_interval = parse_interval(self.args.get("update_interval", "20min"))
        self.mqtt_topic_write = self.args["mqtt_topic_write"]
        self.mqtt_topic_read = self.args["mqtt_topic_read"]
        self.schedule = self.args["schedule"]
        self.low_minsoc = self.args.get("low_batt_minsoc", 100)
        self.med_minsoc = self.args.get("med_batt_minsoc", 200)
        # SM-18: thresholds that turn the static schedule's 'dual' slots into
        # charge / dual-limit / dual based on current SoC.
        self.mode_pick_low_stop_pct = self.args.get("mode_pick_low_stop_pct", 20)
        self.dual_limit_threshold_pct = self.args.get("dual_limit_threshold_pct", 30)
        bypass = self.args.get("bypass_tracker", {})
        self.bypass_debounce_seconds = bypass.get("debounce_seconds", 60)
        self.solar_threshold_w = bypass.get("solar_threshold_w", 50)
        self.fallback_days_when_missing = bypass.get("fallback_days_when_missing", 7)

        # Bypass tracker (BT-1..6)
        self._bypass_pending_handle = None
        self._bootstrap_bypass_timestamp()
        for entity in self.BYPASS_INPUT_SENSORS:
            self.listen_state(self._on_bypass_input_change, entity)

        # Bypass status sensor (BT-7): exposes app-derived vs Zendure-reported
        # bypass agreement as a chartable 4-state string.
        self._last_bypass_status = None
        self.listen_state(
            self._on_zendure_bypass_change, self.BYPASS_ZENDURE_REPORTED_SENSOR
        )
        self._update_bypass_status_sensor()

        # Periodic tick (SM-1, SM-2). Anchor the schedule to clock-aligned
        # minute boundaries (e.g. :00/:20/:40 for a 20 min interval) so ticks
        # happen at predictable wall-clock times across restarts, matching the
        # legacy HA cron schedule. The run_in kickoff still fires once shortly
        # after init so the shadow sensor populates without waiting up to a
        # full interval for the first aligned boundary.
        interval_min = self.update_interval // 60
        next_start = next_aligned_minute(self.datetime(), interval_min)
        self.run_in(self._tick, 1)
        self.run_every(self._tick, next_start, self.update_interval)
        self.log(
            f"ZendureStateMachine started; aligned schedule starts at {next_start.isoformat()}"
        )

    # ------------------------------------------------------------------
    # Bypass tracker (BT-1..6)
    # ------------------------------------------------------------------

    def _bootstrap_bypass_timestamp(self):
        """BT-2: restore last bypass time from HA, or seed a fallback."""
        raw = self.get_state("sensor.zendure_bypass_reached_at")
        if raw and raw not in ("unknown", "unavailable"):
            try:
                self._last_bypass_at = datetime.datetime.fromisoformat(raw)
                return
            except (ValueError, TypeError):
                self.log(
                    f"sensor.zendure_bypass_reached_at unparseable ({raw!r}), using fallback",
                    level="WARNING",
                )
        # Fallback: pretend last bypass was N days ago AND write the sensor so
        # it materializes on the dashboard from t=0 (BT-2 implementation refinement).
        fallback = self.datetime() - datetime.timedelta(days=self.fallback_days_when_missing)
        self._last_bypass_at = fallback
        self._write_bypass_sensor(fallback)
        self.log(
            f"sensor.zendure_bypass_reached_at missing — seeded {fallback.isoformat()}",
            level="WARNING",
        )

    def _write_bypass_sensor(self, ts):
        """BT-3: TZ-aware ISO-8601 with device_class: timestamp."""
        self.set_state(
            "sensor.zendure_bypass_reached_at",
            state=ts.isoformat(),
            attributes={
                "device_class": "timestamp",
                "friendly_name": "Zendure Bypass Reached At",
            },
        )

    def _on_bypass_input_change(self, entity, attribute, old, new, kwargs):
        """BT-5: re-evaluate predicate; debounce True via run_in, cancel if False."""
        if self._evaluate_bypass_predicate():
            if self._bypass_pending_handle is None:
                self._bypass_pending_handle = self.run_in(
                    self._confirm_bypass, self.bypass_debounce_seconds
                )
        elif self._bypass_pending_handle is not None:
            self.cancel_timer(self._bypass_pending_handle)
            self._bypass_pending_handle = None
        self._update_bypass_status_sensor()

    def _on_zendure_bypass_change(self, entity, attribute, old, new, kwargs):
        """BT-7: refresh the status sensor whenever Zendure's reported flag flips."""
        self._update_bypass_status_sensor()

    def _update_bypass_status_sensor(self):
        """BT-7: write sensor.zendure_bypass_active only when the 4-state string changes."""
        app_active = self._evaluate_bypass_predicate()
        zendure_state = self.get_state(self.BYPASS_ZENDURE_REPORTED_SENSOR)
        zendure_active = zendure_state == "True"
        status = bypass_status(app_active, zendure_active)
        if status == self._last_bypass_status:
            return
        self._last_bypass_status = status
        self.set_state(
            "sensor.zendure_bypass_active",
            state=status,
            attributes={
                "friendly_name": "Zendure Bypass Active",
                "app_active": app_active,
                "zendure_active": zendure_active,
            },
        )

    def _evaluate_bypass_predicate(self):
        return is_bypass_active(
            electric_level=self._get_state_int("sensor.zendure_mqtt_electriclevel"),
            packstate=self.get_state("sensor.zendure_mqtt_packstate") or "",
            outputpackpower=self._get_state_int("sensor.zendure_mqtt_outputpackpower"),
            solarinputpower=self._get_state_int("sensor.zendure_mqtt_solarinputpower"),
            solar_threshold=self.solar_threshold_w,
        )

    def _confirm_bypass(self, kwargs):
        """BT-6: re-evaluate after debounce; if still True, latch new timestamp."""
        self._bypass_pending_handle = None
        if self._evaluate_bypass_predicate():
            now = self.datetime()
            self._last_bypass_at = now
            self._write_bypass_sensor(now)
            self.log(f"Bypass reached at {now.isoformat()}")

    # ------------------------------------------------------------------
    # Periodic tick (SM-1..17)
    # ------------------------------------------------------------------

    def _tick(self, kwargs):
        try:
            now = self.datetime()
            old_mode = self.get_state("zendure.operation_mode")
            scheduled_mode = pick_operation_mode(now.hour, self.schedule)
            # SM-18: refine 'dual' to charge/dual-limit/dual based on SoC.
            electric_level = self._get_state_int("sensor.zendure_mqtt_electriclevel")
            new_mode = refine_active_mode(
                scheduled_mode,
                electric_level,
                old_mode,
                self.mode_pick_low_stop_pct,
                self.dual_limit_threshold_pct,
            )

            # SM-7: cold-start. Adopt new_mode silently, no transition payload.
            if old_mode in (None, "unknown", "unavailable"):
                self._write_mode(new_mode)
                return

            # SM-6: real mode change -> ask Zendure for fresh data, defer the
            # mode payload 5 s so the report comes back into HA before we
            # decide on minSoc / passMode based on level / bypass-now.
            if old_mode != new_mode:
                self._publish_mqtt(self.mqtt_topic_read, {"properties": ["getAll"]})
                self.run_in(
                    self._tick_after_getall, 5, old_mode=old_mode, new_mode=new_mode
                )
                return

            # No mode change: still pick a payload (SM-14 bypass-renew case).
            self._tick_send_payload(old_mode, new_mode)
        except Exception as e:
            self.log(f"Error in _tick: {e}", level="ERROR")

    def _tick_after_getall(self, kwargs):
        try:
            self._tick_send_payload(kwargs["old_mode"], kwargs["new_mode"])
        except Exception as e:
            self.log(f"Error in _tick_after_getall: {e}", level="ERROR")

    def _tick_send_payload(self, old_mode, new_mode):
        bypass_now = self._evaluate_bypass_predicate()
        days = (self.datetime() - self._last_bypass_at).days
        electric_level = self._get_state_int("sensor.zendure_mqtt_electriclevel")
        payload, effective_mode = pick_mode_payload(
            old_mode,
            new_mode,
            bypass_now,
            electric_level=electric_level,
            days_since_last_bypass=days,
            low_minsoc=self.low_minsoc,
            med_minsoc=self.med_minsoc,
        )
        if payload is not None:
            self._publish_mqtt(self.mqtt_topic_write, payload)
        if effective_mode != old_mode:
            self.log(f"Mode {old_mode} -> {effective_mode}")
        self._write_mode(effective_mode)

    def _write_mode(self, mode):
        """Write to shadow sensor in dry_run mode, otherwise to the live entity.

        Shadow value is the raw mode string identical to the live entity, so
        Lovelace charts can compare them directly during the prototyping phase.
        """
        if self._dry_run():
            self.set_state(
                "sensor.zendure_operation_mode_shadow",
                state=mode,
                attributes={"friendly_name": "Zendure Operation Mode (shadow)"},
            )
        else:
            self.set_state(
                "zendure.operation_mode",
                state=mode,
                attributes={"friendly_name": "Zendure Operation Mode"},
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_state_int(self, entity_id, default=0):
        val = self.get_state(entity_id)
        if val in (None, "unknown", "unavailable"):
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    def _dry_run(self):
        """Default to True (safe / shadow) when the helper is missing."""
        state = self.get_state("input_boolean.zendure_dry_run")
        if state in (None, "unknown", "unavailable"):
            return True
        return state == "on"

    def _publish_mqtt(self, topic, payload):
        payload_str = json.dumps(payload)
        # In dry_run, redirect to a shadow-prefixed topic with the same payload
        # so an external subscriber can diff our proposed writes against the
        # live python_script's writes on the real topic.
        if self._dry_run():
            topic = f"shadow/{topic}"
        try:
            self.call_service(
                "mqtt/publish", topic=topic, payload=payload_str
            )
        except Exception as e:
            self.log(f"MQTT publish failed: {e}", level="ERROR")
