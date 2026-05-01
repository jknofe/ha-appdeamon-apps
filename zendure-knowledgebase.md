# Zendure SolarFlow → AppDaemon Knowledgebase

Working document for the migration of the existing Home Assistant `python_script.*` Zendure logic into AppDaemon apps living next to `PowerMeter.py`.

## Goal

Move the running-in-HA Zendure SolarFlow control logic into AppDaemon. Keep the surface area visible from HA (sensors, helpers, dashboards) unchanged. No new credentials; reuse HA's existing MQTT integration.

## Scope

In scope:
- Port `zendure_setpoint.py` → AppDaemon `ZendureSetpoint`
- Port `zendure_state_machine.py` → AppDaemon `ZendureStateMachine`
- Replace the unreliable `automation.zendure_bypass_reached` + `sensor.zendure_mqtt_bypass` chain with an AppDaemon-side bypass tracker, hosted inside `ZendureStateMachine`

Out of scope:
- `zendure_state_decoder.py` — already redundant. The HA YAML decodes `packState` 0/1/2 into `idle/charging/discharging`. No consumer reads the decoder's output.
- `zendure_battery_state.py` — stub.
- `power_*.py` and `engery_meter_totals.py` — superseded by `PowerMeter.py`.
- `configuration_mqtt_sensors.yaml` — stays in HA, AppDaemon reads `sensor.zendure_mqtt_*` states.
- `input_select.zendure_operation_mode_strategy` — future feature for manual override (force-serve / force-charge / etc.). See parking lot.

## Reference files

| Path | Role |
| --- | --- |
| `ha-appdeamon-apps/PowerMeter.py` | Reference AppDaemon style |
| `ha-appdeamon-apps/apps.yaml` | App manifest, append two new entries |
| `zendure-solarflow-control/zendure_setpoint.py` | Source of setpoint logic |
| `zendure-solarflow-control/zendure_state_machine.py` | Source of state-machine logic |
| `zendure-solarflow-control/configuration_mqtt_sensors.yaml` | HA-side MQTT decoding, kept as-is |

## Design decisions

| # | Decision | Rationale |
| --- | --- | --- |
| Q1 | **Two AppDaemon apps**: `ZendureSetpoint`, `ZendureStateMachine`. Decoder dropped. | Schedules differ (20 s vs 20 min); decoder is dead code. |
| Q2 | **MQTT publish via HA service** (`self.call_service("mqtt/publish", topic=…, payload=…)`). | No broker credentials in AppDaemon; matches existing scripts. |
| Q3 | **MQTT subscribe / decoding stays in HA YAML**. AppDaemon reads `sensor.zendure_mqtt_*` states. | Lowest risk. |
| Q4 | **Persistent flags stay as HA entities** where still used. The new live scripts have stripped most of them. | Visible on dashboards, restored after HA restart. |
| Q5 | **Hybrid config**: device-identity in `apps.yaml`, two HA helpers (`input_boolean.zendure_dry_run`, `input_number.zendure_inverter_max_power`) for live tuning. Other constants stay in `apps.yaml`. | No restart for tuning; minimal helper sprawl. |
| Q6 | **Shadow mode for the whole prototyping phase**. No MQTT publish to Zendure, no overwrite of live `sensor.zendure_setpoint` / `zendure.operation_mode`. Writes parallel `*_shadow` sensors. Cutover via `input_boolean.zendure_dry_run`. | Inverter is driven every 20 s; bug = wrong power flow. |
| Q7 | **Bypass tracker lives inside `ZendureStateMachine`**, uses `listen_state` + 60 s debounce on the conjunction `electriclevel==100 ∧ packstate=='idle' ∧ outputpackpower==0 ∧ solarinputpower>50`. Persists to `sensor.zendure_bypass_reached_at`. | Replaces the unreliable `sensor.zendure_mqtt_bypass` and the dumb HA automation; keeps app count at two. |

## MQTT topics

- **Read** (decoded by HA YAML): `/73bkTV/SE7546CU/properties/report`
- **Write** (publish via `mqtt/publish` service):
  - `iot/73bkTV/SE7546CU/properties/write` — payloads: `{"properties": {"outputLimit": <int>}}` (setpoint), `{"properties": {"passMode": <0|1>, "minSoc": <int>, "outputLimit": 0}}` (state-machine transitions)
  - `iot/73bkTV/SE7546CU/properties/read` — `{"properties": ["getAll"]}` (state-machine, on mode change)

## HA sensors consumed (inputs)

**Setpoint inputs**
- `sensor.power_consumption`, `sensor.power_import` — produced by `PowerMeter.py`
- `sensor.hm_400_power` — solar inverter power
- `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_outputpackpower`, `sensor.zendure_mqtt_solarinputpower`, `sensor.zendure_mqtt_packstate`
- `sensor.zendure_setpoint` — previous value (change detection)
- `zendure.operation_mode`

**State-machine inputs**
- `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_outputpackpower`, `sensor.zendure_mqtt_packinputpower`, `sensor.zendure_mqtt_solarinputpower`, `sensor.zendure_mqtt_packstate`
- `sensor.zendure_bypass_reached_at` — read on tick to compute `days_since_last_bypass`; written by the tracker inside the same app
- `zendure.operation_mode`

## HA entities written (outputs)

**Always**
- `sensor.zendure_bypass_reached_at` (`device_class: timestamp`) — bypass tracker

**During shadow mode**
- `sensor.zendure_setpoint_shadow`
- `sensor.zendure_operation_mode_shadow`

**After cutover** (additionally)
- `sensor.zendure_setpoint`
- `zendure.operation_mode`

Persistent flags **dropped** by the new live scripts and therefore not reproduced: `zendure.operation_mode_msg`, `zendure.hours_since_last_bypass`, `zendure.batt_low_stop`, `zendure.battery_discharged`.

## HA helpers to create

| Helper | Purpose | Default | Range |
| --- | --- | --- | --- |
| `input_boolean.zendure_dry_run` | Shadow-mode kill switch / panic switch | `on` | on / off |
| `input_number.zendure_inverter_max_power` | Cap on `outputLimit` outside `dual` mode | 390 | 0–1500, step 30 |

If a helper is missing or `unknown`/`unavailable`, the app falls back to the `apps.yaml` default.

## `apps.yaml` config

```yaml
zendure_setpoint:
  module: ZendureSetpoint
  class: ZendureSetpoint
  update_interval: "20s"          # parse_interval accepts "20s", "20min", "1h", or bare int
  mqtt_topic_write: "iot/73bkTV/SE7546CU/properties/write"
  inverter_max_power_default: 390
  dual_mode_max_power: 600
  dual_mode_solar_margin: 60     # half_solar_power = solarInputPower - margin
  power_step: 30
  batt_low_stop: 10              # %
  power_target_bias_steps: 0.5   # subtract this many steps from power_target

zendure_state_machine:
  module: ZendureStateMachine
  class: ZendureStateMachine
  update_interval: "20min"
  mqtt_topic_write: "iot/73bkTV/SE7546CU/properties/write"
  mqtt_topic_read:  "iot/73bkTV/SE7546CU/properties/read"
  schedule:                       # 24-slot list, hour → mode
    [serve, serve, serve, serve, serve, serve,
     charge, charge,
     dual, dual, dual, dual, dual, dual, dual,
     serve, serve, serve, serve, serve, serve, serve, serve, serve]
  low_batt_minsoc: 100            # 10 % * 10
  med_batt_minsoc: 200            # 20 % * 10
  bypass_tracker:
    debounce_seconds: 60
    solar_threshold_w: 50
    fallback_days_when_missing: 7
```

## Behaviour summary

### Setpoint (every 20 s)

1. Read consumption / solar / battery / mqtt-derived sensors and `zendure.operation_mode`.
2. Derive `bypass_mode = (outputpackpower == 0) AND (packstate == 'idle')`.
3. `power_target = power_con − power_sol − (power_step * power_target_bias_steps)`. Quantize to `power_step`.
4. Apply mode override:
   - `charge` → setpoint = 0
   - `dual` → cap inverter to `dual_mode_max_power` (600); compute `half_solar_power = (solarInputPower − dual_mode_solar_margin) quantized`; `setpoint = min(setpoint, half_solar_power)`
   - default → cap inverter to `inverter_max_power` helper (or default)
5. Battery protection: if `electriclevel ≤ batt_low_stop` → setpoint = 0 (no latch, no hysteresis — matches new live behavior).
6. Clamp `0 ≤ setpoint ≤ cap`.
7. If changed since last publish → publish MQTT `outputLimit`. Always update `sensor.zendure_setpoint` (or `*_shadow`).

### State-machine (every 20 min, plus on AppDaemon start)

1. Look up `new_operation_mode = schedule[now.hour]`.
2. On mode change vs `zendure.operation_mode`: publish `getAll` MQTT, schedule the mode payload 5 s later via `self.run_in` (non-blocking).
3. Read `sensor.zendure_bypass_reached_at` (fall back to "now − fallback_days" if missing). Compute `days_since_last_bypass`.
4. Pick `mqtt_command` and possibly delay the transition:
   - `→ serve`: `bypass_now → outputLimit:0, passMode:1, minSoc:low_batt_minsoc`; elif `level ≥ 30 ∧ days < 7 → outputLimit:0, minSoc:med_batt_minsoc`; else delay (revert mode).
   - `→ dual`: delay if `level < 20 ∧ days < 7`.
   - `→ charge`: `outputLimit:0, passMode:0, minSoc:low_batt_minsoc`.
   - no-change but `bypass_now`: `outputLimit:0, passMode:0, minSoc:low_batt_minsoc`.
5. Publish `mqtt_command` if any. Update `zendure.operation_mode` (or `*_shadow`).

### Bypass tracker (event-driven, hosted in `ZendureStateMachine`)

1. `initialize()` registers `listen_state` on `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_packstate`, `sensor.zendure_mqtt_outputpackpower`, `sensor.zendure_mqtt_solarinputpower`.
2. On each event, evaluate predicate `is_bypass_active(electriclevel, packstate, outputpackpower, solarinputpower)` = `electriclevel == 100 AND packstate == 'idle' AND outputpackpower == 0 AND solarinputpower > solar_threshold_w`.
3. If True and no debounce timer pending → `self.run_in(_confirm_bypass, debounce_seconds)`. If False → cancel any pending timer.
4. `_confirm_bypass`: re-evaluate predicate; if still True, write `now()` (ISO timestamp) to `sensor.zendure_bypass_reached_at`.
5. On `initialize()`, read `sensor.zendure_bypass_reached_at` to bootstrap `self._last_bypass_at`. If missing/`unknown`/`unavailable` → fallback to `now − fallback_days`.

## Risk notes / parking lot

- `time.sleep(5)` after a mode-change `getAll` was blocking in the original. AppDaemon should use `self.run_in(_send_mode_payload, 5)` instead so the callback returns promptly.
- The `dual` mode rule `min(setpoint, (solar−60) quantized)` and the `power_target_bias_steps = 0.5` half-step downward bias are recent tuning by the user. Port verbatim. Revisit after shadow-mode comparison; consider promoting to HA helpers if tuning continues.
- The 60 s debounce / 50 W solar threshold for the bypass tracker are guesses. Verify against historical data once the tracker is live; tune if false positives or misses occur.
- `input_select.zendure_operation_mode_strategy` (options: auto / optimze / full-charge-first / force-serve / force-bypass / force-charge) is reserved for a future "manual override" feature on top of `ZendureStateMachine`. Not wired up in this migration.
- Dropped flags (`zendure.battery_discharged`, `zendure.hours_since_last_bypass`, `zendure.batt_low_stop`, `zendure.operation_mode_msg`) may still appear stale in the HA recorder. Backlog: decide whether to delete the entities or leave them to age out.

### Implementation refinements

- **Bootstrap the bypass-timestamp on first init.** When `sensor.zendure_bypass_reached_at` is missing, the in-memory fallback is "now − fallback_days_when_missing", AND we immediately `set_state` it so the sensor exists on the dashboard from t=0.
- **Cold-start `zendure.operation_mode` may be `unknown` / `unavailable` / `None`.** State-machine glue must treat that as "same as new_mode" — no transition payload, just write the current mode. Tests cover this case.
- **Shadow setpoint format must mirror the live format byte-for-byte.** The live script writes `repr(round(setpoint, 0))` → `"30.0"`. The shadow path writes the same string so chart comparison is clean. Same for `sensor.zendure_operation_mode_shadow` — write the raw mode string identical to `zendure.operation_mode`.
- **`device_class: timestamp` requires a TZ-aware ISO-8601 string.** Use `self.datetime().isoformat()` (AppDaemon returns TZ-aware). Never write a naive `datetime.now()`.
- **AppDaemon-created sensors survive HA restarts via the recorder DB**, not via YAML declaration. Existing `sensor.power_consumption` etc. already rely on this and it works — we follow precedent. Risk: if recorder is purged or the sensor is unwritten for `purge_keep_days` (default 10), it can disappear. Mitigation if it ever bites: declare them as MQTT discovery sensors, but not now.
