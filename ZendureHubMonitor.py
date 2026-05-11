"""Zendure SolarFlow bypass tracker + one-time firmware init.

Two responsibilities:

  1. Bypass tracker (event-driven via listen_state):
     Detect when the battery has fully cycled (electric_level == 100,
     packstate == 'idle', outputpackpower == 0, solar passing through).
     Debounce 60 s, then latch the timestamp into
     sensor.zendure_bypass_reached_at — ZendureSetpoint reads this to
     decide the post-bypass deep-drain window and the weekly force-charge.

  2. One-time firmware init (5 s after start):
     Send {minSoc, passMode, outputLimit:0} so the firmware-side hard
     floor is at 10 %. The setpoint loop enforces the higher 10–20 %
     soft floor via outputLimit.

Plus a 4-state diagnostic sensor.zendure_bypass_active that exposes our
predicate vs Zendure's reported `pass` flag (written only on flips).
"""
import datetime
import json

import appdaemon.plugins.hass.hassapi as hass


BYPASS_REPORTED_SENSOR = "sensor.zendure_mqtt_bypass"


def is_bypass_active(soc, packstate, outputpackpower, solarinputpower, solar_threshold):
    """Bypass predicate. Strict > on solar: irradiance noise hovers near the
    threshold at low light; >= would false-trigger constantly."""
    return (soc == 100
            and packstate == 'idle'
            and outputpackpower == 0
            and solarinputpower > solar_threshold)


def bypass_status(app_active, zendure_active):
    """Combine our predicate with Zendure's reported `pass` flag.
    Returns 'none' / 'app_only' / 'zendure_only' / 'both'."""
    if app_active and zendure_active:
        return "both"
    if app_active:
        return "app_only"
    if zendure_active:
        return "zendure_only"
    return "none"


class ZendureHubMonitor(hass.Hass):

    def initialize(self):
        a = self.args
        self.mqtt_topic_write = a["mqtt_topic_write"]
        bypass = a.get("bypass_tracker", {})
        self.debounce_seconds           = bypass.get("debounce_seconds", 60)
        self.solar_threshold_w          = bypass.get("solar_threshold_w", 50)
        self.fallback_days_when_missing = bypass.get("fallback_days_when_missing", 7)
        fw = a.get("firmware_init", {})
        self.init_min_soc               = fw.get("min_soc", 10)    # % — multiplied ×10 before sending
        self.init_pass_mode             = fw.get("pass_mode", 0)   # normal
        # dry_run is config-only — see ZendureSetpoint.
        self.dry_run                    = bool(a.get("dry_run", True))
        # Only the DC solar input is configurable here (used in the bypass
        # predicate). The other listen_state inputs come from the Zendure
        # HA integration with stable names and stay hardcoded.
        pi = a.get("power_inputs", {})
        self.solar_input_power_sensor   = pi.get("solar_input_power", "sensor.zendure_mqtt_solarinputpower")

        self._pending_handle = None
        self._last_bypass_status = None

        self._bootstrap_bypass_timestamp()
        for entity in (
            "sensor.zendure_mqtt_electriclevel",
            "sensor.zendure_mqtt_packstate",
            "sensor.zendure_mqtt_outputpackpower",
            self.solar_input_power_sensor,
        ):
            self.listen_state(self._on_bypass_input_change, entity)
        self.listen_state(self._on_zendure_reported_change, BYPASS_REPORTED_SENSOR)
        self._update_bypass_status_sensor()

        # Delay 5 s so HA's MQTT integration is fully up before we publish.
        self.run_in(self._send_firmware_init, 5)

        self.log("ZendureHubMonitor started")

    # ------------------------------------------------------------------
    # Bypass tracker
    # ------------------------------------------------------------------

    def _bootstrap_bypass_timestamp(self):
        """Ensure sensor.zendure_bypass_reached_at has a parseable ISO timestamp.
        Without one, the setpoint app sees "never bypassed" forever and the
        weekly force-charge fires every tick."""
        raw = self.get_state("sensor.zendure_bypass_reached_at")
        if raw and raw not in ("unknown", "unavailable"):
            try:
                datetime.datetime.fromisoformat(raw)
                return
            except (ValueError, TypeError):
                self.log(f"bypass_reached_at unparseable ({raw!r}), seeding fallback",
                         level="WARNING")
        seed = self.datetime() - datetime.timedelta(days=self.fallback_days_when_missing)
        self._write_bypass_sensor(seed)
        self.log(f"bypass_reached_at seeded {seed.isoformat()}", level="WARNING")

    def _write_bypass_sensor(self, ts):
        # device_class=timestamp requires TZ-aware ISO-8601.
        self.set_state(
            "sensor.zendure_bypass_reached_at",
            state=ts.isoformat(),
            attributes={
                "device_class": "timestamp",
                "friendly_name": "Zendure Bypass Reached At",
            },
        )

    def _on_bypass_input_change(self, entity, attribute, old, new, kwargs):
        """Re-evaluate predicate. Start debounce on True; cancel pending on False."""
        if self._evaluate_predicate():
            if self._pending_handle is None:
                self._pending_handle = self.run_in(self._confirm_bypass, self.debounce_seconds)
        elif self._pending_handle is not None:
            self.cancel_timer(self._pending_handle)
            self._pending_handle = None
        self._update_bypass_status_sensor()

    def _on_zendure_reported_change(self, entity, attribute, old, new, kwargs):
        self._update_bypass_status_sensor()

    def _confirm_bypass(self, kwargs):
        """Debounce callback: re-check predicate; if still True, latch timestamp."""
        self._pending_handle = None
        if self._evaluate_predicate():
            now = self.datetime()
            self._write_bypass_sensor(now)
            self.log(f"Bypass reached at {now.isoformat()}")

    def _evaluate_predicate(self):
        return is_bypass_active(
            soc=self._get_state_int("sensor.zendure_mqtt_electriclevel"),
            packstate=self.get_state("sensor.zendure_mqtt_packstate") or "",
            outputpackpower=self._get_state_int("sensor.zendure_mqtt_outputpackpower"),
            solarinputpower=self._get_state_int(self.solar_input_power_sensor),
            solar_threshold=self.solar_threshold_w,
        )

    # ------------------------------------------------------------------
    # Diagnostic status sensor
    # ------------------------------------------------------------------

    def _update_bypass_status_sensor(self):
        app_active = self._evaluate_predicate()
        zendure_active = self.get_state(BYPASS_REPORTED_SENSOR) == "True"
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

    # ------------------------------------------------------------------
    # One-time firmware init
    # ------------------------------------------------------------------

    def _send_firmware_init(self, kwargs):
        """Set the firmware's persistent minSoc + passMode once. minSoc is
        the hard discharge floor — keep it at the lowest meaningful value
        (10 %) and let the setpoint loop enforce the higher soft floor.
        outputLimit:0 puts the inverter in a safe state until the setpoint
        loop's first tick."""
        payload = {"properties": {
            "minSoc": self.init_min_soc * 10,
            "passMode": self.init_pass_mode,
            "outputLimit": 0,
        }}
        self._publish_mqtt(self.mqtt_topic_write, payload)
        self.log(f"Firmware init sent: {payload}")

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

    def _publish_mqtt(self, topic, payload):
        payload_str = json.dumps(payload)
        if self.dry_run:
            topic = f"shadow/{topic}"
        try:
            self.call_service("mqtt/publish", topic=topic, payload=payload_str)
        except Exception as e:
            self.log(f"MQTT publish failed: {e}", level="ERROR")
