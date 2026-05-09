"""Zendure SolarFlow bypass tracker + one-time firmware init.

Lean rewrite. The legacy state machine's mode-picking has moved into
ZendureSetpoint (decided per 20-s tick from current state, no separate
cadence needed). This app now owns just two responsibilities:

  1. Bypass tracker (event-driven via listen_state):
     Detect when the battery has fully cycled (electric_level == 100,
     packstate == 'idle', outputpackpower == 0, solar passing through).
     Debounce 60 s; then latch the timestamp into
     sensor.zendure_bypass_reached_at, which ZendureSetpoint reads to
     decide the post-bypass deep-drain window and the weekly force-charge.

  2. One-time firmware init (5 s after start):
     Send {minSoc, passMode, outputLimit:0} once so the firmware-side
     hard floor is at 10 % (our app enforces the higher 10–20 % soft
     floor via outputLimit). No runtime per-mode flipping anymore.

Plus a 4-state diagnostic sensor sensor.zendure_bypass_active that
exposes our derived predicate vs Zendure's reported `pass` flag, written
only on flips so HA history stays clean.
"""
import datetime
import json

import appdaemon.plugins.hass.hassapi as hass


BYPASS_INPUT_SENSORS = (
    "sensor.zendure_mqtt_electriclevel",
    "sensor.zendure_mqtt_packstate",
    "sensor.zendure_mqtt_outputpackpower",
    "sensor.zendure_mqtt_solarinputpower",
)
BYPASS_REPORTED_SENSOR = "sensor.zendure_mqtt_bypass"


def is_bypass_active(soc, packstate, outputpackpower, solarinputpower, solar_threshold):
    """Bypass predicate. Strict > on solar — irradiance noise hovers near
    the threshold at low light; >= would false-trigger constantly."""
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


class ZendureStateMachine(hass.Hass):

    def initialize(self):
        a = self.args
        self.mqtt_topic_write = a["mqtt_topic_write"]
        bypass = a.get("bypass_tracker", {})
        self.debounce_seconds           = bypass.get("debounce_seconds", 60)
        self.solar_threshold_w          = bypass.get("solar_threshold_w", 50)
        self.fallback_days_when_missing = bypass.get("fallback_days_when_missing", 7)
        fw = a.get("firmware_init", {})
        self.init_min_soc               = fw.get("min_soc", 100)   # 10 % (Zendure stores ×10)
        self.init_pass_mode             = fw.get("pass_mode", 0)   # normal

        self._pending_handle = None
        self._last_bypass_status = None

        self._bootstrap_bypass_timestamp()
        for entity in BYPASS_INPUT_SENSORS:
            self.listen_state(self._on_bypass_input_change, entity)
        self.listen_state(self._on_zendure_reported_change, BYPASS_REPORTED_SENSOR)
        self._update_bypass_status_sensor()

        # Firmware init delayed 5 s so HA's MQTT integration is fully up
        # after a restart before we publish.
        self.run_in(self._send_firmware_init, 5)

        self.log("ZendureStateMachine started")

    # ------------------------------------------------------------------
    # Bypass tracker
    # ------------------------------------------------------------------

    def _bootstrap_bypass_timestamp(self):
        """Make sure sensor.zendure_bypass_reached_at exists with a parseable
        ISO timestamp on boot. Without it the setpoint app would treat the
        sensor as "never bypassed" forever and weekly-force-charge would fire
        every tick."""
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
            solarinputpower=self._get_state_int("sensor.zendure_mqtt_solarinputpower"),
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
        """Set the Zendure firmware's persistent minSoc + passMode once.
        minSoc is the firmware's hard discharge floor; we keep it at the
        lowest meaningful value (10 %) and let our setpoint loop enforce
        the higher soft floor (10–20 % depending on bypass recency) via
        outputLimit. passMode 0 is normal operation. outputLimit:0 puts
        the inverter in a safe state until the setpoint loop runs."""
        payload = {"properties": {
            "minSoc": self.init_min_soc,
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
        if self._dry_run():
            topic = f"shadow/{topic}"
        try:
            self.call_service("mqtt/publish", topic=topic, payload=payload_str)
        except Exception as e:
            self.log(f"MQTT publish failed: {e}", level="ERROR")

    def _dry_run(self):
        v = self.get_state("input_boolean.zendure_dry_run")
        if v in (None, "unknown", "unavailable"):
            return True  # safe default: shadow
        return v == "on"
