# Zendure → AppDaemon Implementation Tasks

Ordered checklist. Implementation only starts after this list is agreed. Companion docs: `zendure-knowledgebase.md`, `WORKING-STYLE.md`.

**Layout reminder** (per `WORKING-STYLE.md`):
- Pure logic → `zendure_logic.py` (no AppDaemon import; tested with `pytest`)
- AppDaemon glue → `ZendureSetpoint.py`, `ZendureStateMachine.py` (read state, call logic, write state/MQTT)
- Tests → `tests/`

## Phase 0 — Prep (no code yet)

- [ ] Confirm `zendure-knowledgebase.md` matches intent
- [ ] Verify all `sensor.zendure_mqtt_*` are present and updating in HA
- [ ] Confirm the dumb HA bypass automation is no longer needed (will be replaced by AppDaemon-side tracker, can be deleted in Phase 7)

## Phase 1 — HA-side helpers

- [ ] Create HA helpers (UI or `configuration.yaml`):
  - [ ] `input_boolean.zendure_dry_run` (default `on`)
  - [ ] `input_number.zendure_inverter_max_power` (390, 0–1500 step 30)
- [ ] Restart HA, confirm helpers visible

## Phase 2 — Local test scaffold

- [x] On the Mac: install Python 3.14 (HA 2026.4 minimum) via `brew install python@3.14`, create `.venv`, `pip install pytest` inside the venv
- [x] Create `zendure_logic.py` with stub functions raising `NotImplementedError`
- [x] Create `tests/conftest.py` (no `__init__.py` — keeps pytest discovery simple, AppDaemon can `exclude_dirs: [tests]` if it complains)
- [x] Add `.gitignore` for `.venv/`, `__pycache__/`, `.pytest_cache/`
- [x] Verify `.venv/bin/pytest tests/` collects 36 tests (all failing `NotImplementedError`)

## Phase 3 — `ZendureStateMachine` (port first, includes bypass tracker)

### 3a. Pure logic in `zendure_logic.py` + tests
- [ ] Add `is_bypass_active(electric_level, packstate, outputpackpower, solarinputpower, solar_threshold)` — returns bool
- [ ] Test: all four conditions met → True
- [ ] Test: each single condition broken → False (4 cases)
- [ ] Test: `solarinputpower == solar_threshold` is False (uses strict `>`)
- [ ] Add `pick_operation_mode(hour, schedule)` — pure lookup
- [ ] Test: 6:00 → charge, 8:00 → dual, 15:00 → serve, 22:00 → serve
- [ ] Add `pick_mode_payload(old_mode, new_mode, bypass_now, electric_level, days_since_last_bypass, low_minsoc, med_minsoc)` — returns `(mqtt_payload_or_None, effective_new_mode)`
- [ ] Test: `→ serve` with `bypass_now` → outputLimit:0, passMode:1, minSoc:low
- [ ] Test: `→ serve` with `level ≥ 30 ∧ days < 7` → outputLimit:0, minSoc:med
- [ ] Test: `→ serve` neither → delay (effective_new_mode == old_mode, payload None)
- [ ] Test: `→ dual` with `level < 20 ∧ days < 7` → delay
- [ ] Test: `→ dual` with `level ≥ 20` → no payload, mode advances
- [ ] Test: `→ charge` always emits charge payload
- [ ] Test: no mode change but `bypass_now` → emits bypass-low-stop payload
- [ ] Test: no mode change and not bypass → None, None
- [ ] Test: `old_mode` in {None, 'unknown', 'unavailable'} → treated as same-as-new_mode (no transition payload)

### 3b. AppDaemon glue (`ZendureStateMachine.py`)
- [ ] Add `zendure_state_machine` block to `apps.yaml` per knowledgebase
- [ ] Skeleton: `initialize()`, `run_every` for 20 min cadence + run-on-start
- [ ] Helper `_get_state_int(entity, default)` mirroring original
- [ ] Helper `_helper_or_default(helper_id, yaml_key)` for hybrid config
- [ ] Helper `_dry_run()` reading `input_boolean.zendure_dry_run`
- [ ] **Bypass tracker**:
  - [ ] `initialize()` reads `sensor.zendure_bypass_reached_at` to bootstrap `self._last_bypass_at`; if missing/`unknown`/`unavailable` → fall back to `self.datetime() − fallback_days_when_missing` AND immediately `set_state(...)` so the sensor materializes on the dashboard from t=0
  - [ ] All timestamp writes use `self.datetime().isoformat()` (TZ-aware) with `attributes={'device_class': 'timestamp'}`
  - [ ] Register `listen_state` on the four bypass-related sensors
  - [ ] On state event, evaluate `is_bypass_active(...)`; if True and no timer pending → `self.run_in(_confirm_bypass, debounce_seconds)`; if False → cancel pending timer
  - [ ] `_confirm_bypass`: re-evaluate predicate; if still True, set `self._last_bypass_at = self.datetime()` and `set_state("sensor.zendure_bypass_reached_at", ...)`
- [ ] **Periodic tick**:
  - [ ] Read inputs and `old_mode`
  - [ ] `new_mode = pick_operation_mode(now.hour, schedule)`
  - [ ] Treat `old_mode` in {None, 'unknown', 'unavailable'} as same-as-`new_mode` (no transition; just write current)
  - [ ] If mode change → publish MQTT `getAll`; schedule mode payload 5 s later via `self.run_in` (no `time.sleep`)
  - [ ] Compute `days_since_last_bypass` from `self._last_bypass_at`
  - [ ] `payload, effective_mode = pick_mode_payload(...)`; publish if any
  - [ ] Write `zendure.operation_mode` (live) or `sensor.zendure_operation_mode_shadow` — shadow value uses the same raw mode string as the live entity
  - [ ] Gate every MQTT publish on `not _dry_run()`
- [ ] Smoke run on HA: load app, watch logs for one cycle and one bypass event; confirm shadow sensor updates and bypass timestamp populates

## Phase 4 — `ZendureSetpoint`

### 4a. Pure logic (extend `zendure_logic.py`) + tests
- [ ] Add `derive_bypass_now(outputpackpower, packstate)` — returns bool
- [ ] Test: `(0, 'idle')` → True; everything else → False
- [ ] Add `compute_setpoint(power_con, power_sol, mode, solar_input_power, electric_level, batt_low_stop, inverter_max_power, dual_max_power, dual_solar_margin, power_step, target_bias_steps)` — returns int
- [ ] Test: serve mode, target quantizes to step
- [ ] Test: half-step bias subtracted (e.g. `bias=0.5, step=30 → target − 15`)
- [ ] Test: `charge` mode → 0
- [ ] Test: `dual` mode caps at `min(dual_max_power, (solar − margin) quantized)`
- [ ] Test: `dual` mode with `solar < margin` → cap is 0 (or negative, then clamped)
- [ ] Test: `electric_level ≤ batt_low_stop` → 0 (no latch)
- [ ] Test: clamp `0 ≤ setpoint ≤ cap`

### 4b. AppDaemon glue (`ZendureSetpoint.py`)
- [ ] Add `zendure_setpoint` block to `apps.yaml`
- [ ] Skeleton mirroring `PowerMeter.py`: `initialize()`, `_is_running` guard, `run_every(..., 20)`
- [ ] Reuse helpers `_get_state_int`, `_helper_or_default`, `_dry_run` (factor into a shared module if duplication itches)
- [ ] Read input sensors and `zendure.operation_mode`
- [ ] Call `derive_bypass_now(...)` and `compute_setpoint(...)`
- [ ] Change-detect against `setpoint_old` (read from `sensor.zendure_setpoint` once at startup, then track in-memory)
- [ ] Write outputs:
  - shadow path → `sensor.zendure_setpoint_shadow` (state string formatted as `repr(round(setpoint, 0))` to match live exactly for chart comparison)
  - live path → `sensor.zendure_setpoint` (same formatting) + `mqtt/publish` to `properties/write`
- [ ] Gate every MQTT publish on `not _dry_run()`

## Phase 5 — Shadow-mode verification (≥ 24 h)

- [ ] Both apps loaded, `input_boolean.zendure_dry_run` = `on`
- [ ] HA python_script automations still active (live path)
- [ ] Lovelace chart: `sensor.zendure_setpoint` vs `sensor.zendure_setpoint_shadow`
- [ ] Lovelace chart: `zendure.operation_mode` vs `sensor.zendure_operation_mode_shadow`
- [ ] Watch ≥ 24 h covering all schedule transitions (serve → charge → dual → serve)
- [ ] Verify `sensor.zendure_bypass_reached_at` updates on a real bypass moment
- [ ] Reconcile every persistent diff > `power_step` or any operation-mode disagreement
- [ ] Re-test until traces align

## Phase 6 — Cutover

- [ ] Disable / delete the HA automations triggering the python_scripts (setpoint every 20 s, state-machine every 20 min) — likely in `python_scripts/automations.yaml` on the HA host
- [ ] Flip `input_boolean.zendure_dry_run` = `off`
- [ ] Watch live path for one full schedule cycle
- [ ] Verify Zendure receives MQTT (observe power flow change or Zendure cloud reflection)

## Phase 7 — Cleanup (minimal — backlog the rest)

- [ ] Archive or remove `zendure_setpoint.py`, `zendure_state_machine.py`, `zendure_state_decoder.py`, `zendure_battery_state.py` from `zendure-solarflow-control/`
- [ ] Remove the dumb "battery 100 % long enough" HA automation (replaced by AppDaemon bypass tracker)

## Backlog (not part of this migration)

- Locate and remove the `python_script.zendure_*` triggers from the HA `python_scripts/automations.yaml` include
- Remove obsolete `power_consumption.py` / `power_solargen.py` / `engery_meter_totals.py` from HA (covered by `PowerMeter.py`)
- Remove `*_shadow` sensors after a week of stable live operation
- Delete stale HA entities `zendure.operation_mode_msg`, `zendure.hours_since_last_bypass`, `zendure.batt_low_stop`, `zendure.battery_discharged`, `sensor.zendure_mqtt_bypass`
- Wire `input_select.zendure_operation_mode_strategy` into `ZendureStateMachine` for manual override (force-serve / force-charge / etc.)
- Decide whether to promote the `dual` half-power tunables (`dual_mode_max_power`, `dual_mode_solar_margin`) and `power_target_bias_steps` from `apps.yaml` to live HA helpers
- Re-tune bypass tracker thresholds (`debounce_seconds = 60`, `solar_threshold_w = 50`) based on observed history

## Deployment workflow (resolved)

- Edit on Mac → `git push` to GitHub → `git pull` on the HA host (where this repo is checked out as / inside the AppDaemon `apps/` directory) → AppDaemon's file watcher auto-reloads the changed `.py` files and `apps.yaml`.
- The `tests/` directory sits inside the apps tree but is not referenced from `apps.yaml`, so AppDaemon ignores it for app loading. If AppDaemon logs spurious warnings about unreferenced files, that's cosmetic — not a functional problem.
- Implication for commit cadence: any commit pushed during shadow-mode prototyping will be picked up live by AppDaemon on the next `git pull`. Make sure each pushed commit is loadable (per `WORKING-STYLE.md`).

## Done criteria

- AppDaemon `ZendureSetpoint` and `ZendureStateMachine` are the sole writers of `sensor.zendure_setpoint` and `zendure.operation_mode`.
- `sensor.zendure_bypass_reached_at` is the canonical bypass-time source, updated by the AppDaemon tracker.
- No Zendure-related `python_script.*` runs in HA.
- Re-enabling `input_boolean.zendure_dry_run` cleanly stops MQTT writes (panic switch verified).
