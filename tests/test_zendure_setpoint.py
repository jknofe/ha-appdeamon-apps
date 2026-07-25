"""Tests for ZendureSetpoint's publish gating.

The pure functions are covered in test_app_helpers.py. What is exercised here
is the composition in `_tick` that implements the acceptance criterion: a
publish that HA does not confirm must not advance `_setpoint_old`, and must not
be laundered into `sensor.zendure_setpoint` either - `initialize()` bootstraps
`_setpoint_old` from that sensor, so writing an unconfirmed value there would
resurrect the desync across an AppDaemon reload.
"""
import datetime

from ZendureSetpoint import ZendureSetpoint


NOW = datetime.datetime(2026, 7, 25, 12, 0, 0)

# soc 50, consumption 600, no secondary solar, no DC solar in, bypass 1 h ago:
#   floor         -> 10 (inside the post-bypass window)
#   charge latch  -> False (50 > 10)
#   mode          -> 'free' (soc 50 >= soc_promote 30)
#   setpoint      -> (600 - 0 - 15) // 30 * 30 = 570
EXPECTED_SETPOINT = 570


class FakeSetpoint(ZendureSetpoint):
    """ZendureSetpoint with the AppDaemon surface stubbed out.

    `initialize()` is never called - the config attributes it would set are
    assigned directly, which keeps the test independent of AppDaemon's
    scheduler and of HA state.
    """

    def __init__(self, publish_results, setpoint_old):
        self._publish_results = list(publish_results)
        self.published = []
        self.logs = []
        self.written = {}
        self.states = {
            "sensor.zendure_mqtt_electriclevel": "50",
            "sensor.power_consumption": "600",
            "sensor.hm_400_power": "0",
            "sensor.zendure_mqtt_solarinputpower": "0",
            "sensor.zendure_bypass_reached_at": (NOW - datetime.timedelta(hours=1)).isoformat(),
        }
        # Config normally assigned by initialize()
        self.mqtt_topic_write = "iot/test/properties/write"
        self.max_cap = 720
        self.power_step = 30
        self.bias_steps = 0.5
        self.floor_after_bypass = 10
        self.floor_default = 20
        self.soc_promote = 30
        self.solar_threshold_w = 100
        self.weekly_force_hours = 174
        self.dry_run = False
        self.power_consumption_sensor = "sensor.power_consumption"
        self.solar_secondary_power_sensor = "sensor.hm_400_power"
        self.solar_input_power_sensor = "sensor.zendure_mqtt_solarinputpower"
        self._charge_latch = False
        self._free_latch = False
        self._mode_old = None
        self._is_running = False
        self._publish_failing = False
        self._setpoint_old = setpoint_old

    # --- AppDaemon surface ---

    def get_state(self, entity_id, **kwargs):
        return self.states.get(entity_id)

    def set_state(self, entity_id, state=None, attributes=None):
        self.written[entity_id] = state
        self.states[entity_id] = state

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def datetime(self):
        return NOW

    def call_service(self, service, **kwargs):
        self.published.append(kwargs.get("payload"))
        return self._publish_results.pop(0)


def make(publish_results, setpoint_old=300):
    return FakeSetpoint(publish_results, setpoint_old)


OK = {"success": True, "ad_status": "OK"}
FAIL = {"success": False, "error": {"code": "home_assistant_error", "message": "not connected"}}


def test_confirmed_publish_advances_tracker():
    app = make([OK])
    app._tick(None)
    assert app._setpoint_old == EXPECTED_SETPOINT
    assert len(app.published) == 1


def test_failed_publish_does_not_advance_tracker():
    app = make([FAIL])
    app._tick(None)
    assert app._setpoint_old == 300


def test_failed_publish_does_not_write_unconfirmed_value_to_sensor():
    # The sensor is the bootstrap source after a reload. Writing 570 here
    # would make initialize() believe the hub holds 570 when it holds 300.
    app = make([FAIL])
    app._tick(None)
    # Pins both halves: not the unconfirmed value, and specifically the last
    # confirmed one - so the assertion cannot pass by writing nothing at all.
    assert app.states["sensor.zendure_setpoint"] == "300"


def test_sensor_mirrors_confirmed_value_after_success():
    app = make([OK])
    app._tick(None)
    assert app.states["sensor.zendure_setpoint"] == repr(round(EXPECTED_SETPOINT, 0))


def test_next_tick_retries_after_failure():
    app = make([FAIL, OK])
    app._tick(None)
    app._tick(None)
    assert len(app.published) == 2
    assert app._setpoint_old == EXPECTED_SETPOINT


def test_outage_logs_once_not_every_tick():
    app = make([FAIL, FAIL, FAIL])
    for _ in range(3):
        app._tick(None)
    errors = [m for lvl, m in app.logs if lvl == "ERROR"]
    assert len(errors) == 1


def test_recovery_is_logged_once():
    app = make([FAIL, OK])
    app._tick(None)
    app._tick(None)
    recovered = [m for lvl, m in app.logs if "confirmed again" in m]
    assert len(recovered) == 1


def test_no_publish_when_setpoint_unchanged():
    app = make([], setpoint_old=EXPECTED_SETPOINT)
    app._tick(None)
    assert app.published == []


def test_publish_payload_is_single_property():
    app = make([OK])
    app._tick(None)
    assert app.published[0] == '{"properties": {"outputLimit": 570}}'


def test_shadow_mode_routes_to_shadow_topic():
    app = make([OK])
    app.dry_run = True
    captured = {}

    def call_service(service, **kwargs):
        captured.update(kwargs)
        return OK

    app.call_service = call_service
    app._tick(None)
    assert captured["topic"] == "shadow/iot/test/properties/write"
