# Zendure AppDaemon Requirements

Concrete, testable spec for the lean rewrite. Companion: `zendure-knowledgebase.md` (rationale).

Every requirement has a stable ID. Tests reference the IDs in their docstrings so coverage is traceable.

---

## 1. Scope

Three AppDaemon apps:
- `ZendureSetpoint` - computes `outputLimit` every 20 s, writes mode and setpoint sensors.
- `ZendureHubMonitor` - event-driven bypass tracker + one-time firmware init.
- `EnergyMeterTotals` - cumulative solar yield every 5 min (observational only).

Legacy `python_script.*` automations and scripts have been removed from HA.

Out of scope: `zendure_state_decoder.py`, `zendure_battery_state.py`, `power_*.py` (covered by `PowerMeter.py`), future `input_select.zendure_operation_mode_strategy`.

## 2. Glossary

| Symbol | Meaning |
|---|---|
| `power_step` | Quantization step for setpoint, default 30 W |
| `max_cap` | outputLimit cap in `free` mode, default 720 W |
| `floor` | Effective SoC % below which setpoint is forced to 0. Dynamic per SP-floor. |
| `batt_floor_after_bypass` | Floor inside the post-bypass window, default 10 % |
| `batt_floor_default` | Floor outside the post-bypass window, default 20 % |
| `POST_BYPASS_WINDOW_HOURS` | Class constant. Length of post-bypass window during which `after_bypass` floor applies. Fixed 10 h. |
| `charge_latch` | In-memory bool. Engages at SoC <= floor; releases at SoC >= floor + `LATCH_HYSTERESIS_PCT`. While latched, setpoint is 0. |
| `LATCH_HYSTERESIS_PCT` | Class constant. Recovery margin before charge_latch releases. Fixed 5 %. |
| `free_latch` | In-memory bool. Daily drain commitment. Engages at SoC >= `soc_promote`; cleared when charge_latch engages. |
| `soc_promote` | SoC at/above which we commit to `free` mode for the cycle, default 30 % |
| `solar_threshold_w` | DC solar input above this is considered real daylight, default 100 W |
| `weekly_charge_force_hours` | Hours since last bypass that force `charge` mode, default 174 (7.5 d) |
| `hours_since_last_bypass` | now - `sensor.zendure_bypass_reached_at`, in hours |
| `dry_run` | apps.yaml bool flag. `true` = shadow mode (MQTT to `shadow/<topic>`, HA writes to `*_shadow` sensors). |

## 3. Cross-cutting requirements

- **CC-1** All apps subclass `appdaemon.plugins.hass.hassapi.Hass`.
- **CC-2** All MQTT publishes via `self.call_service("mqtt/publish", topic=..., payload=...)`. No direct broker connection.
- **CC-3** Every MQTT publish and HA control-sensor write is gated on `dry_run`. With `dry_run=true`: MQTT goes to `shadow/<topic>`, HA writes go to `*_shadow` sensors. Live topics and sensors are never touched while `dry_run=true`.
- **CC-4** `initialize()` is idempotent and cheap (no blocking I/O >= 100 ms).
- **CC-5** Periodic callbacks guard against reentry with `self._is_running`.
- **CC-6** All HA reads tolerate `None`/`'unknown'`/`'unavailable'` by returning a documented default; never raise.
- **CC-7** No `time.sleep`. Delayed actions use `self.run_in(callback, seconds)`.
- **CC-8** `self.datetime()` (TZ-aware) for all timestamps. Never `datetime.datetime.now()`.

## 4. `ZendureSetpoint` requirements

### Cadence and lifecycle
- **SP-1** Runs every `update_interval`, default `"20s"` (parsed by `app_helpers.parse_interval`).
- **SP-2** First tick fires at `start + update_interval` via `run_every(..., "now", ...)`. No separate `run_in` kickoff -- 20 s is short enough that the delay is invisible.

### Inputs
- **SP-3** Reads each tick (all tolerant of missing/unavailable):
  - `sensor.zendure_mqtt_electriclevel` (int %) -- battery SoC
  - `sensor.power_consumption` (int W, configurable) -- home load
  - `sensor.hm_400_power` (int W, configurable as `solar_secondary_power`) -- uncontrolled inverter AC, subtracted from demand
  - `sensor.zendure_mqtt_solarinputpower` (int W, configurable as `solar_input_power`) -- DC into Zendure hub
  - `sensor.zendure_bypass_reached_at` -- own output, parsed for `hours_since_last_bypass`

### Floor computation (pure function `effective_floor`)
- **SP-floor-1** Returns `batt_floor_after_bypass` when `hours_since_last_bypass < POST_BYPASS_WINDOW_HOURS`, else `batt_floor_default`. Strict `<`.
- **SP-floor-2** Re-evaluated every tick. Not cached.

### Charge latch (pure function `update_charge_latch`)
- **SP-latch-1** Engages (`True`) when `soc <= floor`. Once latched, stays `True` until `soc >= floor + LATCH_HYSTERESIS_PCT`.
- **SP-latch-2** While latched, `compute_setpoint` returns 0 (mode = `'charge'` path is used; the caller forces mode override is not needed -- the latch is checked before mode caps are applied).
- **SP-latch-3** When charge_latch transitions True, `free_latch` is cleared (floor hit resets the daily drain commitment).
- **SP-latch-4** Bootstraps from `sensor.zendure_battery_discharged` (or `*_shadow` fallback) on `initialize()`.

### Mode picking (pure function `pick_mode`)
- **SP-mode** Decision order (first match wins):
  1. `hours_since_bypass >= weekly_charge_force_hours` -> `'charge'`, `free_latch=False`
  2. `charge_latched` -> `'charge'`, `free_latch=False`
  3. `free_latch` already on -> `'free'`, latch carried
  4. `soc >= soc_promote` -> `'free'`, `free_latch=True`
  5. `solar_input > solar_threshold_w` -> `'solar-only'`, `free_latch=False`
  6. else -> `'free'`, `free_latch=False` (mid-SoC, no real sun -- battery is the only buffer)

  Returns `(mode, new_free_latch, reason)`. `reason` is a human-readable string naming the rule and key values, used when the caller logs mode transitions.

### Setpoint computation (pure function `compute_setpoint`)
- **SP-compute-charge** `mode == 'charge'` -> return 0 immediately.
- **SP-compute-target** Raw target: `consumption - solar_secondary - (power_step * bias_steps)`. Half-step bias (`bias_steps=0.5`) shifts the quantized floor down by half a step -- errs toward slight grid import rather than export.
- **SP-compute-quantize** Quantize: `(raw_target // power_step) * power_step`.
- **SP-compute-cap** `mode == 'solar-only'`: cap = `(solar_input // power_step) * power_step`, min 0. `mode == 'free'`: cap = `max_cap`.
- **SP-compute-clamp** `setpoint = max(0, min(quantized, cap))`. Return as `int`.

### Outputs
- **SP-out-setpoint** Write setpoint to `sensor.zendure_setpoint` (live) or `sensor.zendure_setpoint_shadow` (shadow). State as `repr(round(setpoint, 0))` (e.g. `"30.0"`). Attributes: `state_class: measurement`, `unit_of_measurement: W`, `device_class: power`. Skip write if entity already holds the same state string (avoids a HA state-changed event every 20 s for a stable setpoint).
- **SP-out-mode** Write mode string to `sensor.zendure_operation_mode` (live) or `sensor.zendure_operation_mode_shadow` (shadow). Skip write if unchanged.
- **SP-out-discharged** Write `"True"`/`"False"` to `sensor.zendure_battery_discharged` (live) or `sensor.zendure_battery_discharged_shadow` (shadow) only when the latch value flips.
- **SP-out-mqtt** Publish `{"properties": {"outputLimit": <int>}}` to `mqtt_topic_write` only when setpoint changed since last publish. In-memory tracker bootstrapped from `sensor.zendure_setpoint` on first cycle.

## 5. `ZendureHubMonitor` requirements

### Bypass tracker setup
- **BT-1** Bypass tracker hosted in `ZendureHubMonitor.initialize()`. Runs event-driven, not on a timer.
- **BT-2** Bootstrap on init: read `sensor.zendure_bypass_reached_at`. If parseable ISO timestamp -> no-op. If missing/`unknown`/`unavailable`/unparseable -> write `now() - fallback_days_when_missing` and log WARNING.
- **BT-3** All timestamp writes use `self.datetime().isoformat()` with attributes `{device_class: timestamp, friendly_name: 'Zendure Bypass Reached At'}`. TZ-aware.

### Bypass predicate (pure function `is_bypass_active`)
- **BT-4** `soc == 100 AND packstate == 'idle' AND outputpackpower == 0 AND solarinputpower > solar_threshold_w`. Strict `>` on solar: irradiance noise near the threshold would cause false triggers with `>=`.

### Debounce loop (latched state machine)
- **BT-5** In-memory latch `_bypass_active` (False on init). `listen_state` registered on: `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_packstate`, `sensor.zendure_mqtt_outputpackpower`, `solar_input_power_sensor` (configurable). On any change: re-evaluate predicate. If predicate disagrees with `_bypass_active` and no timer pending -> `self._pending_handle = self.run_in(_confirm_transition, debounce_seconds)`. If predicate agrees with `_bypass_active` (back to latched state) and timer pending -> `cancel_timer`, clear handle. The initialize() path calls the state-machine entry once after wiring listeners so a bypass already in progress at startup is picked up without waiting for an input change.
- **BT-6** `_confirm_transition`: clear handle, re-evaluate predicate. If predicate True and latch False -> flip latch to True, write `now()` to `sensor.zendure_bypass_reached_at`, log INFO `Bypass started at <iso>`. If predicate False and latch True -> flip latch to False, log INFO `Bypass ended at <iso>` -- DO NOT write the timestamp on the end transition. Otherwise (predicate flipped back during the debounce window) -> no log, no write. No in-memory timestamp cache -- next read comes from the sensor.
- **BT-6a** While the latch is True, repeated True evaluations from input changes neither log nor rearm the debounce timer. This is the anti-spam invariant: at most one `Bypass started` and one `Bypass ended` per bypass cycle, regardless of how many input updates arrive during the bypass.
- **BT-6b** `sensor.zendure_bypass_reached_at` is written at most once per bypass cycle, on the confirmed start transition. The sensor name reads literally ("when bypass was *reached*") so a single write per cycle keeps the semantics intact and avoids continuous recorder churn observed previously.

### Diagnostic status sensor (pure function `bypass_status`)
- **BT-7** `sensor.zendure_bypass_active` maintained by `bypass_status(app_active, zendure_active)`:
  - `'none'` -- neither true
  - `'app_only'` -- our predicate true, Zendure silent (the case we work around)
  - `'zendure_only'` -- Zendure true, our predicate disagrees (worth reviewing)
  - `'both'` -- agreement

  Updated on every predicate-input change AND on `sensor.zendure_mqtt_bypass` change, and once on `initialize()`. Written only when the state string flips. Attributes carry raw `app_active` / `zendure_active`. Not gated by `dry_run`.

### Firmware init
- **FI-1** `self.run_in(_send_firmware_init, 5)` called in `initialize()`. Delayed 5 s so HA's MQTT integration is fully up.
- **FI-2** Payload: `{"properties": {"minSoc": min_soc * 10, "passMode": pass_mode, "outputLimit": 0}}`. Published once via `_publish_mqtt` (which applies dry_run routing). Sets the firmware's hard discharge floor as a last-resort safety net; `ZendureSetpoint` enforces the higher soft floor via `outputLimit`.
- **FI-3** `min_soc` from `apps.yaml` in percent; app multiplies x10 before sending to Zendure. Config value is human-readable.

## 6. `EnergyMeterTotals` requirements

- **EM-1** Runs every `update_interval`, default `"5m"` (parsed by `app_helpers.parse_interval`).
- **EM-2** Sums all sensors in the `sensors` list (float kWh each) and adds `legacy_kwh_offset` (fixed kWh from decommissioned inverters -- see `apps.yaml` comment for breakdown).
- **EM-3** If any sensor value is `None`/`'unknown'`/`'unavailable'`, skip the tick silently. No stale value written.
- **EM-4** Write `sensor.power_meter_solar_total` (`state_class: total_increasing`, `unit_of_measurement: kWh`, `device_class: energy`) only when the rounded-to-one-decimal value changes.
- **EM-5** No `dry_run` gate -- purely observational, no control effect.

## 7. Configuration requirements

### `apps.yaml`
- **CFG-1** `update_interval` (parsed by `app_helpers.parse_interval`) for `ZendureSetpoint` and `EnergyMeterTotals`.
- **CFG-2** `mqtt_topic_write` for the device's MQTT write topic.
- **CFG-3** Setpoint constants: `max_cap`, `power_step`, `power_target_bias_steps`, `batt_floor_after_bypass`, `batt_floor_default`, `soc_promote_to_free`, `solar_threshold_w`, `weekly_charge_force_hours`. `POST_BYPASS_WINDOW_HOURS` and `LATCH_HYSTERESIS_PCT` are class constants, not config keys.
- **CFG-4** `dry_run` (bool) in both `zendure_setpoint` and `zendure_hub_monitor`. Both values must match.
- **CFG-5** `power_inputs` YAML anchor: `power_consumption`, `solar_primary_power`, `solar_secondary_power`, `solar_input_power`. Shared between both apps.
- **CFG-6** Bypass tracker: `bypass_tracker.debounce_seconds`, `bypass_tracker.solar_threshold_w`, `bypass_tracker.fallback_days_when_missing`.
- **CFG-7** Firmware init: `firmware_init.min_soc` (percent), `firmware_init.pass_mode`.
- **CFG-8** EnergyMeterTotals: `sensors` list, `legacy_kwh_offset` (float kWh).

### HA host
- **CFG-9** `/config/appdaemon.yaml` must include `appdaemon.exclude_dirs: [tests, tools]`. AppDaemon's hot-reload watcher imports every `.py` under `app_dir` including subdirectories; without this, every push that touches a test file emits a non-fatal stack trace.

## 8. Persistence requirements

- **PS-1** `sensor.zendure_bypass_reached_at` is the canonical bypass-time source. Survives HA restarts via the recorder DB. Bootstrap fallback (BT-2) covers the case where it is missing or stale.
- **PS-2** `sensor.zendure_setpoint` survives restarts; bootstraps `setpoint_old` on first cycle for change-detection.
- **PS-3** `charge_latch` bootstraps from `sensor.zendure_battery_discharged` (or `*_shadow` fallback) on `initialize()` so a restart mid-discharge does not briefly re-enable drain.
- **PS-4** `free_latch` is in-memory only. Not persisted -- re-derived from the first tick. Worst case after restart: one tick in `solar-only` instead of `free` if SoC is between `floor` and `soc_promote`. Acceptable.

## 9. Error handling requirements

- **EH-1** Missing/unknown HA inputs fall back to documented defaults; ticks must not crash (CC-6).
- **EH-2** `sensor.zendure_bypass_reached_at` unparseable on bootstrap -> log WARNING, apply fallback (BT-2).
- **EH-3** MQTT publish failure -> log ERROR, do not raise, do not retry within the same tick. Next tick recomputes and publishes if still different.
- **EH-4** Uncaught exception in a periodic callback -> log ERROR with traceback, reset `_is_running` in `finally`, next tick proceeds normally.

## 10. Logging requirements

- **LOG-1** Match `PowerMeter.py` discipline: terse, mostly silent on the happy path.
- **LOG-2** Log INFO on: app start (`<App> started`), mode transition (`Mode <old> -> <new>: <reason>`), bypass start (`Bypass started at <iso>`), bypass end (`Bypass ended at <iso>`), firmware init sent. No log during continuous bypass between start and end.
- **LOG-3** Log WARNING on: bypass-tracker bootstrap fallback, unparseable bypass timestamp.
- **LOG-4** Log ERROR on: MQTT publish failures, caught exceptions.
- **LOG-5** No per-tick INFO. Setpoint must stay quiet at 20 s cadence.

## 11. Test requirements

Two layers. Layer 1 unit-tests the pure functions from the Mac via `pytest`. Layer 2 verifies behaviour on the HA host via shadow mode.

Pure functions live in `ZendureSetpoint.py` and `ZendureHubMonitor.py`. Tests import them directly.

### Layer 1 - `pytest` unit tests in `tests/`

Each test references the requirement ID in its name and docstring.

#### `effective_floor` (SP-floor)
- **TST-1** `hours < window` -> after-bypass floor
- **TST-2** `hours == window` -> default floor (strict `<` boundary)
- **TST-3** `hours > window` -> default floor

#### `update_charge_latch` (SP-latch)
- **TST-4** latch off, `soc > floor` -> stays off
- **TST-5** latch off, `soc == floor` -> engages (`<=`)
- **TST-6** latch off, `soc < floor` -> engages
- **TST-7** latch on, `soc < floor + hysteresis` -> holds
- **TST-8** latch on, `soc == floor + hysteresis` -> releases (boundary: held at `<`, released at `>=`)
- **TST-9** latch on, `soc > floor + hysteresis` -> releases

#### `pick_mode` (SP-mode)
- **TST-10** `hours_since >= weekly_force` -> `('charge', False, <reason>)`
- **TST-11** `charge_latched=True` -> `('charge', False, <reason>)`, free_latch input ignored
- **TST-12** `free_latch=True`, no higher priority -> `('free', True, <reason>)`
- **TST-13** `soc >= soc_promote` -> `('free', True, <reason>)`, latch engaged
- **TST-14** mid-soc, `solar_input > solar_threshold` -> `('solar-only', False, <reason>)`
- **TST-15** mid-soc, `solar_input <= solar_threshold` -> `('free', False, <reason>)` (rule 6)
- **TST-16** weekly force overrides charge_latch and free_latch
- **TST-17** charge_latch overrides free_latch

#### `compute_setpoint` (SP-compute)
- **TST-18** `mode='charge'` -> 0 regardless of inputs
- **TST-19** `mode='free'`, normal target -> quantized within `max_cap`
- **TST-20** `mode='free'`, large target -> clamped at `max_cap`
- **TST-21** `mode='free'`, negative target -> 0
- **TST-22** `mode='solar-only'`, target < solar cap -> target wins
- **TST-23** `mode='solar-only'`, target > solar cap -> solar cap
- **TST-24** `mode='solar-only'`, `solar_input == 0` -> setpoint = 0
- **TST-25** bias of 0.5 steps reduces result relative to unbiased target

#### `is_bypass_active` (BT-4)
- **TST-26** All four conditions met -> True
- **TST-27** `soc < 100` -> False
- **TST-28** `packstate != 'idle'` -> False
- **TST-29** `outputpackpower > 0` -> False
- **TST-30** `solarinputpower == solar_threshold` (boundary, strict `>`) -> False
- **TST-31** `solarinputpower == solar_threshold + 1` -> True

#### `bypass_status` (BT-7)
- **TST-32** `(False, False)` -> `'none'`
- **TST-33** `(True, False)` -> `'app_only'`
- **TST-34** `(False, True)` -> `'zendure_only'`
- **TST-35** `(True, True)` -> `'both'`

### Layer 2 - shadow-mode integration on HA

- **TST-INT-1** Both apps load on AppDaemon without errors after `git pull`.
- **TST-INT-2** With `dry_run=true`, `sensor.zendure_setpoint_shadow` and `sensor.zendure_operation_mode_shadow` populate within one cycle (<=20 s).
- **TST-INT-3** With `dry_run=true`, no MQTT on live topics; payloads appear on `shadow/iot/73bkTV/SE7546CU/properties/write`.
- **TST-INT-4** Over >= 24 h, mode transitions and setpoints are consistent with the pick_mode decision rules across schedule variations and SoC changes.
- **TST-INT-5** A real bypass event (battery 100 % under sun) updates `sensor.zendure_bypass_reached_at` within `debounce_seconds + tolerance`, and `sensor.zendure_bypass_active` flips to `app_only` or `both`.

## 12. Acceptance criteria

- **AC-1** All Layer 1 tests pass via `pytest` from repo root, no warnings.
- **AC-2** `dry_run=false` on both apps; AppDaemon is the sole writer of `sensor.zendure_setpoint`, `sensor.zendure_operation_mode`, and `sensor.zendure_battery_discharged`.
- **AC-3** AppDaemon log is quiet on the happy path (no recurring per-tick messages).
- **AC-4** `sensor.zendure_bypass_reached_at` updates correctly across at least one observed real-world bypass event.
- **AC-5** `/config/appdaemon.yaml` has `exclude_dirs: [tests, tools]`.
