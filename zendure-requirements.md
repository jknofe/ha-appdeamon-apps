# Zendure → AppDaemon Requirements

Concrete, testable requirements for the implementation. Companion docs: `zendure-knowledgebase.md` (design rationale), `zendure-tasks.md` (sequenced work), `WORKING-STYLE.md` (conventions).

Each requirement has a stable ID. Tests reference these IDs in their docstrings/names so coverage is traceable.

---

## 1. Scope

Two AppDaemon apps replace the live `python_script.zendure_setpoint` and `python_script.zendure_state_machine`. A third concern — bypass detection — is hosted inside `ZendureStateMachine` and replaces both `automation.zendure_bypass_reached` and `sensor.zendure_mqtt_bypass`.

Out of scope: the decoder/battery-state stubs, the existing `power_*.py` scripts (covered by `PowerMeter.py`), the future `input_select.zendure_operation_mode_strategy` manual-override.

## 2. Glossary

| Symbol | Meaning |
| --- | --- |
| `power_step` | Quantization step for setpoint, default 30 W |
| `inverter_max_power` | Default cap on `outputLimit`, helper-overridable, default 390 W |
| `dual_max_power` | Cap in `dual` mode, fixed 600 W |
| `dual_solar_margin` | Margin subtracted from solar input in `dual`, fixed 60 W |
| `batt_low_stop` | SoC % below which setpoint is forced to 0, fixed 10 |
| `low_minsoc` | minSoC value used in low-stop MQTT payloads, fixed 100 (= 10 %) |
| `med_minsoc` | minSoC value used in medium-stop MQTT payloads, fixed 200 (= 20 %) |
| `power_target_bias_steps` | Steps subtracted from raw power target, fixed 0.5 |
| `bypass_now` | Instantaneous bypass guess inside `ZendureSetpoint` (`outputpackpower==0 ∧ packstate=='idle'`) |
| `bypass_reached` | Sustained bypass condition recorded by the tracker |
| `dry_run` | Boolean from `input_boolean.zendure_dry_run`, redirects MQTT publishes to `shadow/<topic>` and HA writes to shadow sensors |

## 3. Cross-cutting requirements

- **CC-1** Both apps subclass `appdaemon.plugins.hass.hassapi.Hass`, follow the style of `PowerMeter.py`.
- **CC-2** All MQTT publishes go through `self.call_service("mqtt/publish", topic=..., payload=...)`. No native broker connection.
- **CC-3** Every MQTT publish is gated on `dry_run`. With `dry_run == off`, the payload is published to the configured topic. With `dry_run == on`, the same payload is published to a shadow-prefixed topic (`shadow/<original-topic>`) so an external subscriber can diff our proposed publishes against the live `python_script` writes on the real topic. The live (non-shadow) topic is never written while `dry_run == on`.
- **CC-4** `initialize()` is idempotent and cheap (no blocking I/O ≥ 100 ms, no side effects we wouldn't want repeated on AppDaemon hot-reload).
- **CC-5** Periodic callbacks guard against in-flight reentry with a per-app `self._is_running` flag, mirroring `PowerMeter.py:33`.
- **CC-6** All HA entity reads tolerate `None`, `'unknown'`, `'unavailable'` by returning a documented default (typically `0` for numeric, `''` for string), never raising.
- **CC-7** No `time.sleep`. Any "wait then act" flow uses `self.run_in(callback, seconds)`.
- **CC-8** `self.datetime()` (TZ-aware) is used for all timestamps. Never `datetime.datetime.now()`.
- **CC-9** Pure logic lives in `zendure_logic.py` with no AppDaemon imports. AppDaemon classes are thin glue: read state → call pure function → write state / publish MQTT.

## 4. `ZendureSetpoint` requirements

### Cadence and lifecycle
- **SP-1** Runs every `update_interval`, default `"20s"` (parsed by `app_helpers.parse_interval`).
- **SP-2** First run is kicked off ~1 s after `initialize()` via `run_in`, so the shadow sensor populates without waiting a full cycle. The periodic `run_every` schedule fires from there.

### Inputs
- **SP-3** Reads (in this order, each tolerant of missing/unavailable):
  - `sensor.power_consumption` (int W) — produced by `PowerMeter.py`
  - `sensor.power_import` (int W) — produced by `PowerMeter.py`
  - `sensor.hm_400_power` (int W) — solar inverter
  - `sensor.zendure_mqtt_electriclevel` (int %)
  - `sensor.zendure_mqtt_outputpackpower` (int W)
  - `sensor.zendure_mqtt_solarinputpower` (int W)
  - `sensor.zendure_mqtt_packstate` (string: `idle` / `charging` / `discharging`)
  - `zendure.operation_mode` (string: `serve` / `charge` / `dual`; treat `unknown`/`unavailable` as `serve`)
  - `sensor.zendure_setpoint` (last published value, used for change detection only on first cycle)

### Bypass-now derivation
- **SP-4** `bypass_now = (outputpackpower == 0) AND (packstate == 'idle')`.

### Setpoint computation (pure function `compute_setpoint`)
- **SP-5** Raw target: `power_target = power_consumption − power_solar − (power_step * power_target_bias_steps)`.
- **SP-6** Quantize: `setpoint = (power_target // power_step) * power_step`, integer.
- **SP-7** `mode == 'charge'` overrides setpoint to 0.
- **SP-8** `mode == 'dual'` applies cap = `dual_max_power` (600) AND `setpoint = min(setpoint, half_solar)` where `half_solar = ((solarinputpower − dual_solar_margin) // power_step) * power_step`. If `half_solar < 0`, treat as 0.
- **SP-9** Other modes use cap = `inverter_max_power` (helper-overridable).
- **SP-10** Battery protection: `electriclevel ≤ batt_low_stop` → setpoint = 0. No latch, no hysteresis.
- **SP-11** Final clamp: `0 ≤ setpoint ≤ cap`.

### Outputs
- **SP-12** Setpoint is written to `sensor.zendure_setpoint` (live) or `sensor.zendure_setpoint_shadow` (shadow), state formatted as `repr(round(setpoint, 0))` to match the original script byte-for-byte. Attributes: `state_class: measurement`, `unit_of_measurement: W`, `device_class: power`, `friendly_name: 'Zendure Setpoint' / 'Zendure Setpoint (shadow)'`.
- **SP-13** MQTT publish to `mqtt_topic_write` with payload `{"properties": {"outputLimit": <int>}}` only when `setpoint != setpoint_old` (in-memory tracker; on first cycle, bootstrap from `sensor.zendure_setpoint`).

## 5. `ZendureStateMachine` requirements

### Cadence and lifecycle
- **SM-1** Runs every `update_interval`, default `"20min"` (parsed by `app_helpers.parse_interval`).
- **SM-2** First tick is kicked off ~1 s after `initialize()` via `run_in` (after the bypass tracker is set up), so `sensor.zendure_operation_mode_shadow` populates on cold start without waiting a full 20 min cycle.

### Inputs
- **SM-3** Reads (each tolerant of missing/unavailable):
  - `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_outputpackpower`, `sensor.zendure_mqtt_packinputpower`, `sensor.zendure_mqtt_solarinputpower`, `sensor.zendure_mqtt_packstate`
  - `zendure.operation_mode` (current mode; `unknown`/`unavailable`/`None` treated per CC-6)
  - `sensor.zendure_bypass_reached_at` (own output, read for `days_since_last_bypass` calc)

### Schedule (pure function `pick_operation_mode`)
- **SM-4** 24-slot list from `apps.yaml`. Default: hours 0–5 → `serve`, 6–7 → `charge`, 8–14 → `dual`, 15–23 → `serve`.
- **SM-5** `new_mode = schedule[now.hour]` — pure lookup, no SoC dependency.

### Mode-change protocol
- **SM-6** If `new_mode != old_mode` AND `old_mode` is a known mode (not `None`/`unknown`/`unavailable`):
  - Publish `getAll` to `mqtt_topic_read` with payload `{"properties": ["getAll"]}`
  - Schedule the mode payload via `self.run_in(send_mode_payload, 5)` — non-blocking
- **SM-7** If `old_mode` is unknown, no `getAll` request; treat as same-as-`new_mode` (no transition; just write current).

### Transition guards and payloads (pure function `pick_mode_payload`)
- **SM-8** `→ serve` with `bypass_now` → payload `{"properties": {"outputLimit": 0, "passMode": 1, "minSoc": low_minsoc}}`, mode advances.
- **SM-9** `→ serve` with `electriclevel ≥ 30 ∧ days_since_last_bypass < 7` → `{"properties": {"outputLimit": 0, "minSoc": med_minsoc}}`, mode advances.
- **SM-10** `→ serve` neither → mode does NOT advance (returns `effective_mode = old_mode`), no payload.
- **SM-11** `→ dual` with `electriclevel < 20 ∧ days_since_last_bypass < 7` → mode does NOT advance, no payload.
- **SM-12** `→ dual` otherwise → no payload, mode advances.
- **SM-13** `→ charge` → `{"properties": {"outputLimit": 0, "passMode": 0, "minSoc": low_minsoc}}`, mode advances.
- **SM-14** No mode change but `bypass_now` (current mode) → `{"properties": {"outputLimit": 0, "passMode": 0, "minSoc": low_minsoc}}`.
- **SM-15** No mode change and not `bypass_now` → no payload.

### Outputs
- **SM-16** Effective mode written to `zendure.operation_mode` (live) or `sensor.zendure_operation_mode_shadow` (shadow). Shadow value is the same raw mode string for chart comparison.
- **SM-17** MQTT payloads from SM-8/9/13/14 published to `mqtt_topic_write` only when non-`None`.

## 6. Bypass tracker requirements

### Setup
- **BT-1** Hosted inside `ZendureStateMachine.initialize()`.
- **BT-2** Bootstrap: read `sensor.zendure_bypass_reached_at`. If parseable ISO timestamp → set `self._last_bypass_at`. If missing/`unknown`/`unavailable`/unparseable → fall back to `self.datetime() − fallback_days_when_missing` AND immediately `set_state(...)` so the sensor materializes from t=0.
- **BT-3** All timestamp writes use `self.datetime().isoformat()` (TZ-aware) with attributes `{'device_class': 'timestamp', 'friendly_name': 'Zendure Bypass Reached At'}`.

### Detection (pure function `is_bypass_active`)
- **BT-4** Predicate: `electric_level == 100 AND packstate == 'idle' AND outputpackpower == 0 AND solarinputpower > solar_threshold_w`. Strict `>`.

### Debounce loop
- **BT-5** `listen_state` registered on the four predicate inputs. On any change:
  - Re-evaluate predicate against current values.
  - If True and no debounce timer pending → `self._pending_bypass_handle = self.run_in(_confirm_bypass, debounce_seconds)`.
  - If False and timer pending → `self.cancel_timer(self._pending_bypass_handle)`, clear handle.
- **BT-6** `_confirm_bypass`: re-evaluate predicate against current values. If still True → set `self._last_bypass_at = self.datetime()` AND `set_state("sensor.zendure_bypass_reached_at", state=<iso>, attributes={...})`. Clear handle.

## 7. Configuration requirements

### `apps.yaml` (per knowledgebase block)
- **CFG-1** `update_interval` (duration string or int seconds, parsed by `app_helpers.parse_interval`) for tick cadence in both apps.
- **CFG-2** `mqtt_topic_write`, `mqtt_topic_read` for the device's MQTT topics.
- **CFG-3** `inverter_max_power_default`, `dual_mode_max_power`, `dual_mode_solar_margin`, `power_step`, `batt_low_stop`, `power_target_bias_steps` for setpoint constants.
- **CFG-4** `schedule` (24-slot list), `low_batt_minsoc`, `med_batt_minsoc` for state-machine constants.
- **CFG-5** `bypass_tracker.debounce_seconds`, `bypass_tracker.solar_threshold_w`, `bypass_tracker.fallback_days_when_missing`.

### HA helpers
- **CFG-6** `input_boolean.zendure_dry_run` — dry-run gate per CC-3. Default `on`.
- **CFG-7** `input_number.zendure_inverter_max_power` — overrides `inverter_max_power_default` (for non-dual modes only). If missing/`unknown` → fall back to `apps.yaml` default.

## 8. Persistence requirements

- **PS-1** `sensor.zendure_bypass_reached_at` is the canonical bypass-time source after migration. Survives HA restarts via the recorder DB.
- **PS-2** `sensor.zendure_setpoint` is restored from recorder on AppDaemon restart; first cycle bootstraps `setpoint_old` from it for change detection.
- **PS-3** No app keeps state on disk outside of HA entities. All in-memory state is recoverable from HA on `initialize()`.

## 9. Error handling requirements

- **EH-1** Per CC-6, missing/unknown HA inputs fall back to documented defaults; setpoint/state-machine ticks must not crash.
- **EH-2** If `sensor.zendure_bypass_reached_at` is unparseable on bootstrap, log WARNING and use the fallback per BT-2.
- **EH-3** `mqtt/publish` service-call failure: log ERROR, do not raise, do not retry within the same tick. Next tick will recompute and (if still different) publish again.
- **EH-4** Any uncaught exception in a periodic callback is logged ERROR with traceback, `_is_running` is reset in `finally`, next tick proceeds normally.

## 10. Logging requirements

- **LOG-1** Match `PowerMeter.py` logging discipline: terse, mostly silent on the happy path.
- **LOG-2** Log at INFO on: AppDaemon load (`<App> started`), mode transition (`Zendure mode <old> → <new>, payload=<...>`), bypass-reached event (`Bypass reached at <iso>`). Shadow-mode publishes are not logged per-tick — they are observable by subscribing to `shadow/#`.
- **LOG-3** Log at WARNING on: input parse fallbacks (`<entity> unparseable, using default <x>`), bypass-tracker bootstrap fallback.
- **LOG-4** Log at ERROR on: caught exceptions, MQTT publish failures.
- **LOG-5** Do not log every periodic tick. Setpoint app especially must stay quiet at 20 s cadence.

## 11. Test requirements

Two layers per `WORKING-STYLE.md`. Layer 1 unit-tests the pure functions in `zendure_logic.py` from the Mac. Layer 2 verifies behavior on the HA host via shadow mode.

### Layer 1 — `pytest` unit tests in `tests/`

Each test references the requirement ID it covers in its name (e.g. `test_sp5_power_target_bias`).

#### `is_bypass_active` (BT-4)
- **TST-1** All four conditions met → True
- **TST-2** `electric_level == 99` → False
- **TST-3** `packstate == 'charging'` → False
- **TST-4** `outputpackpower == 1` → False
- **TST-5** `solarinputpower == solar_threshold_w` (boundary, strict `>`) → False
- **TST-6** `solarinputpower == solar_threshold_w + 1` → True

#### `pick_operation_mode` (SM-4, SM-5)
- **TST-7** Hours 0, 5 → `serve`
- **TST-8** Hours 6, 7 → `charge`
- **TST-9** Hours 8, 14 → `dual`
- **TST-10** Hours 15, 23 → `serve`

#### `pick_mode_payload` (SM-7 through SM-15)
- **TST-11** old=serve, new=serve, no bypass → (None, serve)
- **TST-12** old=serve, new=serve, `bypass_now` → bypass-low-stop payload, serve (SM-14)
- **TST-13** old=charge, new=serve, `bypass_now` → SM-8 payload, serve
- **TST-14** old=charge, new=serve, level=30, days=3 → SM-9 payload, serve
- **TST-15** old=charge, new=serve, level=29, days=3 → (None, charge) — delay (SM-10)
- **TST-16** old=charge, new=serve, level=30, days=7 → (None, charge) — delay (SM-10)
- **TST-17** old=charge, new=dual, level=19, days=3 → (None, charge) — delay (SM-11)
- **TST-18** old=charge, new=dual, level=20, days=3 → (None, dual) — advance (SM-12)
- **TST-19** old=charge, new=dual, level=19, days=7 → (None, dual) — advance (SM-12)
- **TST-20** old=serve, new=charge → SM-13 payload, charge
- **TST-21** old=`unknown`, new=charge → (None, charge) — same-as-new per SM-7
- **TST-22** old=None, new=dual → (None, dual)

#### `derive_bypass_now` (SP-4)
- **TST-23** `(0, 'idle')` → True
- **TST-24** `(0, 'charging')` → False
- **TST-25** `(0, 'discharging')` → False
- **TST-26** `(50, 'idle')` → False

#### `compute_setpoint` (SP-5 through SP-11)
- **TST-27** Serve mode, `power_con=300, power_sol=0, step=30, bias=0.5` → `300 − 15 = 285`, quantized to `270` (within cap)
- **TST-28** Serve mode, target large, gets clamped at `inverter_max_power=390` → 390
- **TST-29** Serve mode, target negative → 0
- **TST-30** Charge mode → 0 regardless of inputs
- **TST-31** Dual mode, `solar_input=300, margin=60, step=30` → `half_solar=240`; setpoint = `min(quantized_target, 240, 600)` for various inputs
- **TST-32** Dual mode, `solar_input=50, margin=60` → `half_solar < 0` → clamped to 0 → setpoint = 0
- **TST-33** Dual mode, large target with cap=600 → clamped at min(half_solar, 600)
- **TST-34** Battery protection: `electric_level=10, batt_low_stop=10` (≤) → 0
- **TST-35** Battery protection: `electric_level=11, batt_low_stop=10` → unaffected by protection
- **TST-36** No latch: returning to a healthy SoC immediately allows non-zero setpoint (compare to TST-34 then call again with level=20 → not forced to 0)

### Layer 2 — Shadow-mode integration on HA

- **TST-INT-1** Both apps load on AppDaemon without errors after `git pull`.
- **TST-INT-2** With `dry_run = on`, `sensor.zendure_setpoint_shadow` and `sensor.zendure_operation_mode_shadow` populate within one cycle each.
- **TST-INT-3** While `dry_run = on`: no MQTT messages on the live topics (`iot/73bkTV/SE7546CU/properties/{write,read}`) come from AppDaemon — only the legacy `python_script` writes appear there. The same payloads our apps would publish appear on `shadow/iot/73bkTV/SE7546CU/properties/{write,read}` (verified via HA MQTT integration debug or `mosquitto_sub -t 'shadow/#'`).
- **TST-INT-4** Over a ≥ 24 h window covering all 4 schedule transitions (serve↔charge, charge↔dual, dual↔serve), shadow values match live values within ±`power_step` for setpoint and identically for mode.
- **TST-INT-5** A real bypass moment (battery 100 % under sun) triggers `sensor.zendure_bypass_reached_at` to update within `debounce_seconds + tolerance`.
- **TST-INT-6** Toggling `dry_run` to `off` immediately allows the next computed change to publish MQTT (verified by topic observation). Toggling back to `on` immediately suppresses.

## 12. Acceptance criteria

- **AC-1** All Layer 1 tests pass via `pytest` from repo root, no warnings.
- **AC-2** All Layer 2 integration tests pass on the HA host.
- **AC-3** With `dry_run = off`, AppDaemon is the sole writer of `sensor.zendure_setpoint` and `zendure.operation_mode`; the corresponding `python_script.*` automations are disabled or removed.
- **AC-4** `sensor.zendure_bypass_reached_at` updates correctly across at least one observed real-world bypass event.
- **AC-5** Re-enabling `input_boolean.zendure_dry_run` cleanly redirects MQTT publishes from the live topics to `shadow/<topic>` within one tick (panic switch verified — no further writes hit the live topic until the helper is toggled off again).
- **AC-6** AppDaemon log is quiet on the happy path (no recurring per-tick messages from the new apps).
