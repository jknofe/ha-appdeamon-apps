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
    force_weekly_charge,
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
        self.schedule = self._load_schedule(self.args["schedule"])
        self.low_minsoc = self.args.get("low_batt_minsoc", 100)
        self.med_minsoc = self.args.get("med_batt_minsoc", 200)
        # SM-18: thresholds that turn the static schedule's 'dual' slots into
        # charge / dual-limit / dual based on current SoC.
        self.mode_pick_low_stop_pct = self.args.get("mode_pick_low_stop_pct", 20)
        self.dual_limit_threshold_pct = self.args.get("dual_limit_threshold_pct", 30)
        # SM-20: force-charge after this many hours without a confirmed bypass.
        self.weekly_charge_force_hours = self.args.get("weekly_charge_force_hours", 174)
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

    @staticmethod
    def _load_schedule(raw):
        """Accept the apps.yaml `schedule` as a sparse dict {hour: mode}
        (preferred) or a 24-element list (legacy). Sparse dicts are forward-
        filled into a dense {0..23: mode} dict: each hour inherits the mode
        from the highest defined hour ≤ it, wrapping around so hour 0
        inherits from the last defined hour of the (cyclical) previous day.
        Example: {7: 'dual', 15: 'serve'} → 0-6 serve (wrap from 15), 7-14
        dual, 15-23 serve. Empty schedules raise.
        """
        if isinstance(raw, list):
            return {i: v for i, v in enumerate(raw)}
        if not isinstance(raw, dict):
            raise ValueError(f"schedule must be a dict or list, got {type(raw).__name__}")
        if not raw:
            raise ValueError("schedule is empty")
        defined = {int(k): v for k, v in raw.items()}
        bad = [h for h in defined if not 0 <= h <= 23]
        if bad:
            raise ValueError(f"schedule hours must be in 0..23, got {bad}")
        sorted_hours = sorted(defined)
        # wrap-around seed: hour 0 inherits from the highest defined hour
        # if not explicitly set (cyclical day boundary).
        last_mode = defined[sorted_hours[-1]]
        schedule = {}
        for h in range(24):
            if h in defined:
                last_mode = defined[h]
            schedule[h] = last_mode
        return schedule

    # ------------------------------------------------------------------
    # Bypass tracker (BT-1..6)
    # ------------------------------------------------------------------

    def _bootstrap_bypass_timestamp(self):
        """BT-2: ensure sensor.zendure_bypass_reached_at has a parseable value at boot.

        Writes a fallback (now - N days) when the sensor is missing or
        unparseable, so the dashboard materializes from t=0 and downstream
        `_hours_since_last_bypass` reads have something to subtract from. We do
        not cache the parsed value — every tick re-reads the sensor so updates
        from our own bypass tracker AND from any external writer (e.g. the
        legacy `automation.zendure_bypass_reached`) are picked up immediately.
        """
        raw = self.get_state("sensor.zendure_bypass_reached_at")
        if raw and raw not in ("unknown", "unavailable"):
            try:
                datetime.datetime.fromisoformat(raw)
                return
            except (ValueError, TypeError):
                self.log(
                    f"sensor.zendure_bypass_reached_at unparseable ({raw!r}), using fallback",
                    level="WARNING",
                )
        fallback = self.datetime() - datetime.timedelta(days=self.fallback_days_when_missing)
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
            self._write_bypass_sensor(now)
            self.log(f"Bypass reached at {now.isoformat()}")

    # ------------------------------------------------------------------
    # Periodic tick (SM-1..17)
    # ------------------------------------------------------------------

    def _tick(self, kwargs):
        try:
            now = self.datetime()
            # In dry_run, read the shadow mode written by us so the state
            # machine forms a closed loop with itself (mirrors the Q14 fix on
            # the setpoint side). Otherwise old_mode would be whatever the
            # legacy python_script wrote to the live entity, and every tick
            # we'd log a spurious 'Mode <legacy_mode> -> <our_mode>' transition.
            mode_entity = (
                "sensor.zendure_operation_mode_shadow"
                if self._dry_run()
                else "zendure.operation_mode"
            )
            old_mode = self.get_state(mode_entity)
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
            # SM-20: hard override — too long since last bypass forces charge.
            # Read the sensor each tick (not a cached value) so external
            # writers (legacy automation, our own bypass tracker) are picked
            # up immediately. Caching from boot once caused this app to ride
            # on a stale fallback timestamp forever (history-8 / Q15).
            hours_since = self._hours_since_last_bypass()
            new_mode = force_weekly_charge(
                new_mode, hours_since, self.weekly_charge_force_hours
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
        # Same sensor-read-each-tick discipline as _tick; convert the same
        # hours figure to integer days for pick_mode_payload's SM-9 / SM-11
        # 'days < 7' guards.
        days = int(self._hours_since_last_bypass() // 24)
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

        Skips the set_state call when the target entity already holds the same
        mode. Avoids generating an HA state-changed event every 20 min when
        the schedule keeps us in the same mode for hours.
        """
        if self._dry_run():
            target_entity = "sensor.zendure_operation_mode_shadow"
            friendly_name = "Zendure Operation Mode (shadow)"
        else:
            target_entity = "zendure.operation_mode"
            friendly_name = "Zendure Operation Mode"
        if self.get_state(target_entity) == mode:
            return
        self.set_state(target_entity, state=mode, attributes={"friendly_name": friendly_name})

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

    def _hours_since_last_bypass(self):
        """Read sensor.zendure_bypass_reached_at and return hours-since-now.

        Mirrors ZendureSetpoint's helper so both apps re-read each tick and
        share the same TZ-mismatch tolerance: AppDaemon's `self.datetime()`
        is aware iff `time_zone:` is set in `/config/appdaemon.yaml`, but the
        sensor string may be stored naive (e.g. by HA's recorder normalizing
        a `device_class: timestamp` state). When awareness disagrees we coerce
        the parsed value to match `now`'s tzinfo so the subtraction works.

        On any error we return a safely-large value so `force_weekly_charge`
        doesn't fire spuriously — opposite of the boot-time fallback
        behaviour, which deliberately uses a stale-ish timestamp to ENSURE a
        weekly charge fires if no real bypass has been detected.
        """
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
