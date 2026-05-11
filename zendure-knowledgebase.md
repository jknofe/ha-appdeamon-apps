# Zendure SolarFlow → AppDaemon Knowledgebase

Reference for the AppDaemon migration of the Zendure SolarFlow control logic.
Captures *why* the apps look the way they do, in current state. Day-to-day
mechanics live in `zendure-requirements.md` (testable spec), `WORKING-STYLE.md`
(conventions), and the source files themselves.

## Goal

Move the existing `python_script.zendure_*` control loop into AppDaemon, next
to `PowerMeter.py`. Keep HA-visible surface (sensors, helpers, dashboards)
unchanged. No new credentials — reuse HA's MQTT integration.

## Scope

In scope:
- `ZendureSetpoint` AppDaemon app (every 20 s; replaces `python_script.zendure_setpoint`).
- `ZendureHubMonitor` AppDaemon app (every 20 min; replaces `python_script.zendure_state_machine`).
- AppDaemon-side bypass tracker hosted inside `ZendureHubMonitor`
  (replaces both `automation.zendure_bypass_reached` and the unreliable
  `sensor.zendure_mqtt_bypass`).
- `EnergyMeterTotals` AppDaemon app (every 5 min; ports `engery_meter_totals.py`).

Out of scope:
- `zendure_state_decoder.py` — already redundant; HA YAML decodes `packState`.
- `zendure_battery_state.py` — stub.
- `power_*.py` — superseded by `PowerMeter.py`.
- `configuration_mqtt_sensors.yaml` — stays in HA, AppDaemon reads `sensor.zendure_mqtt_*`.
- `input_select.zendure_operation_mode_strategy` — future manual-override feature.

## Design decisions

| # | Decision | Why |
| --- | --- | --- |
| Q1 | Two AppDaemon apps: `ZendureSetpoint`, `ZendureHubMonitor`. Decoder dropped. | Cadences differ (20 s vs 20 min); decoder is dead. |
| Q2 | MQTT publish via HA service `mqtt/publish`. | No broker creds in AppDaemon. |
| Q3 | MQTT subscribe / decoding stays in HA YAML; AppDaemon reads `sensor.zendure_mqtt_*`. | Lowest-risk slice. |
| Q4 | Persistent flags stay as HA entities where useful. | Visible on dashboards, restored after HA restart. |
| Q5 | Config in `apps.yaml`; one HA helper (`input_boolean.zendure_dry_run`) for the live shadow-mode toggle. The legacy `input_number.zendure_inverter_max_power` was dropped — `dual_cap` / `serve_cap` are now in `apps.yaml`. | Helper sprawl wasn't paying off; AppDaemon hot-reloads on `apps.yaml` edits anyway. |
| Q6 | **Shadow mode for the entire prototyping phase.** Live entities (`sensor.zendure_setpoint`, `zendure.operation_mode`) are not written; writes go to `*_shadow` companions. MQTT publishes are redirected to `shadow/<original-topic>` with the exact payload Zendure would receive — an external subscriber can diff us against the legacy `python_script` on the live topic. Cutover via `input_boolean.zendure_dry_run`. | Inverter is driven every 20 s; bug = wrong power flow. |
| Q7 | Bypass tracker inside `ZendureHubMonitor`, `listen_state` + 60 s debounce on `electriclevel==100 ∧ packstate=='idle' ∧ outputpackpower==0 ∧ solarinputpower>50`. Latches `sensor.zendure_bypass_reached_at`. | Replaces unreliable `sensor.zendure_mqtt_bypass` and the dumb HA "battery 100 % long enough" automation; keeps app count at two. |
| Q8 | `dual` schedule slots are battery-active hours; `refine_active_mode` decides charge/dual-limit/dual at runtime based on SoC + previous mode. | Matches the production for-loop refinement. SoC-driven mode picking lets us defend a 30 % floor in bad weather without adding day-of-week / month logic. |
| Q9 | `dual-limit` mode caps output at quantized solar input — output exactly tracks production, battery never drains. Used in the SoC band 20–29 % during dual hours. | "Reach 30 % at least" on overcast days. |
| Q10 | Cap structure is **two values** (`dual_cap=720`, `serve_cap=540`) plus `dual-limit`'s solar-tracking cap. Dropped: `inverter_max_power_default` (390), `dual_max_power` / `dual_solar_margin` and the `half_solar` cap inside `dual`, `bypass_grace_hours` lift, and the `inverter_max_power` HA helper. | Earlier design had four caps + one helper + one bypass-grace override across modes. Once we accepted that `dual-limit` only fires at low SoC (so a bypass-grace lift is structurally unreachable) and that the inverter has a real efficient working range, two static caps cover everything. |
| Q11 | `battery_discharged` latch with 5 % hysteresis. Once SoC ≤ `batt_low_stop`, latch sticks; only releases at `batt_low_stop + 5 %`. | A 1 % SoC bounce was flapping discharge on/off; keeping the latch flat avoids the chatter without trapping us at low SoC forever. |
| Q12 | 174 h (7.5 d) without confirmed bypass force-overrides mode to `charge`. | Ensures weekly full-cycle in winter / multi-day overcast. |
| Q13 | Discharge floor is **dynamic**, picked each tick by `effective_batt_low_stop`: 10 % when `bypass_now` or `hours_since_last_bypass < 10 h`, else 20 %. Functional/non-sticky equivalent of production's `zendure.batt_low_stop` writes from the state machine. | Freshly-charged battery is safe to drain deeper; no recent bypass means keep a 20 % reserve. Re-evaluating per tick avoids the sticky-state bookkeeping of writing/reading `zendure.batt_low_stop`. |
| Q14 | In `dry_run`, `ZendureSetpoint` reads the **shadow** mode entity (`sensor.zendure_operation_mode_shadow`) rather than `zendure.operation_mode`. Cutover swaps it back automatically (live entity in live mode). | Without this, the shadow setpoint reacts to whatever the legacy `python_script` writes to the live mode entity — so SP-7 (`charge → 0`) never fires when our state machine wants `charge` but the legacy script still says `serve`. Real symptom seen: shadow mode `charge` while shadow setpoint computed 240–420 W in serve-mode style (history-6.csv, 2026-05-08 17:00). |
| Q15 | `ZendureHubMonitor` re-reads `sensor.zendure_bypass_reached_at` each tick rather than caching it at boot. Helper `_hours_since_last_bypass` mirrors the setpoint side, including TZ-mismatch coercion. Same dry_run / live mode-entity selection as Q14 applies to `old_mode`. | Original code parsed the sensor once into `self._last_bypass_at` at bootstrap. If the sensor was missing at boot, the bootstrap fallback set the cache to `now − 7 d`; ~7 h later `force_weekly_charge` (174 h threshold) fired and **kept firing every tick forever** because nothing updated the cache. The legacy `automation.zendure_bypass_reached` later wrote a fresh timestamp to the sensor, but the state machine never noticed. Real symptom: 100+ consecutive `Mode <X> -> charge` log lines spanning 32 h (a0d7b954_appdaemon_2026-05-09T04-23-03.114Z.log). The repeated *log line* per tick is a separate Q14-style bug — `old_mode` was read from the live entity (legacy says `serve`) while we wrote `charge` to the shadow, so the apparent transition never settled. |
| Q16 | `solar_primary_power_sensor` defaults to `sensor.zendure_mqtt_outputhomepower` (Zendure-reported DC feed to HM-1500, ~5 % optimistic vs actual HM-1500 AC), **not** a direct HM-1500 AC measurement — even though AC would be more truthful. | Three reasons stack: **(a) Reliability** — Zendure MQTT keeps streaming when OpenDTU freezes; HM-inverter readings disappear with their WiFi. Same logic as why `sensor.hm_400_power` has a fallback path. **(b) Update cadence** — Zendure reports much faster than the HM inverters, so the value is fresher. **(c) Not used in the control law** — `solar_primary` is currently observability-only; the setpoint equation uses `consumption − solar_secondary`, never reads HM-1500 back. So the ~5 % DC-vs-AC gap doesn't propagate anywhere. The chronic ~5 % under-supply at the HM-1500 (≈ 20–50 W typical, depending on setpoint) is real but it's inverter physics, not a sensor choice; it produces a small grid-import bias that blends with the deliberate `power_target_bias_steps: 0.5` and is the safe direction (never causes export). Revisit only if/when closed-loop correction (use HM-1500 actual to true up `outputLimit`) is added — at that point the sensor source becomes load-bearing. |

## MQTT topics

- **Read** (decoded by HA YAML): `/73bkTV/SE7546CU/properties/report`
- **Write** (publish via `mqtt/publish` service):
  - `iot/73bkTV/SE7546CU/properties/write` — payloads: `{"properties": {"outputLimit": <int>}}` (setpoint), `{"properties": {"passMode": 0|1, "minSoc": <int>, "outputLimit": 0}}` (state-machine transitions).
  - `iot/73bkTV/SE7546CU/properties/read` — `{"properties": ["getAll"]}` (state machine, on mode change).

In dry_run mode all publishes go to `shadow/iot/73bkTV/SE7546CU/properties/{write,read}` instead.

## HA entities consumed

**Setpoint inputs**
- `sensor.power_consumption` — produced by `PowerMeter.py`
- `sensor.hm_400_power` — solar inverter; on failure derives from `max(0, sensor.power_solargen - sensor.zendure_mqtt_outputhomepower)` (SP-17)
- `sensor.power_solargen` (fallback only) — Shelly 1PM total inverter AC output, written by `PowerMeter.py`
- `sensor.zendure_mqtt_outputhomepower` (fallback only) — Zendure's home-output reading, ≈ HM-1500 AC
- `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_outputpackpower`, `sensor.zendure_mqtt_solarinputpower`, `sensor.zendure_mqtt_packstate`
- `sensor.zendure_setpoint` (previous published value, change detection only)
- `sensor.zendure_bypass_reached_at` (own output, read for `hours_since_last_bypass`)
- `zendure.operation_mode` (live) **or** `sensor.zendure_operation_mode_shadow` (dry_run) — picked based on `_dry_run()` so shadow setpoint follows shadow mode rather than the legacy script's live-mode write

**State-machine inputs**
- `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_outputpackpower`, `sensor.zendure_mqtt_solarinputpower`, `sensor.zendure_mqtt_packstate`
- `sensor.zendure_mqtt_bypass` (Zendure's reported `pass` flag — feeds the BT-7 diagnostic)
- `sensor.zendure_bypass_reached_at` — re-read each tick (Q15) for `hours_since_last_bypass` and `days_since_last_bypass`
- `zendure.operation_mode` (live) **or** `sensor.zendure_operation_mode_shadow` (dry_run) — picked based on `_dry_run()` so old_mode reflects our own decisions in shadow rather than the legacy script's writes

## HA entities written

**Always (not gated by dry_run)**
- `sensor.zendure_bypass_reached_at` (`device_class: timestamp`) — bypass tracker.
- `sensor.zendure_bypass_active` (4-state: `none` / `app_only` / `zendure_only` / `both`) — diagnostic comparing our derived predicate to Zendure's reported `pass` flag.

**During dry_run**
- `sensor.zendure_setpoint_shadow`
- `sensor.zendure_operation_mode_shadow`
- `sensor.zendure_battery_discharged_shadow` (`True`/`False` string, matches legacy format)

**After cutover (dry_run = off)**
- `sensor.zendure_setpoint`
- `zendure.operation_mode`
- `sensor.zendure_battery_discharged`

## HA helpers

| Helper | Purpose | Default | Range |
| --- | --- | --- | --- |
| `input_boolean.zendure_dry_run` | Shadow-mode kill switch. `on` = shadow, `off` = live. Default `on`. | on | on/off |

If a helper is missing or `unknown`/`unavailable`, the app falls back to its `apps.yaml` default.

## `apps.yaml` config

```yaml
zendure_setpoint:
  module: ZendureSetpoint
  class: ZendureSetpoint
  update_interval: "20s"               # parse_interval: "20s"/"20min"/"1h"/int
  mqtt_topic_write: "iot/73bkTV/SE7546CU/properties/write"
  dual_cap: 720                        # W — cap in 'dual' (battery drains freely)
  serve_cap: 540                       # W — cap in 'serve' (lower than dual_cap so a sudden consumption drop bounds export overshoot)
  power_step: 30
  batt_low_stop_after_bypass: 10       # % — floor inside post-bypass window (drain deeper)
  batt_low_stop_default: 20            # % — floor outside post-bypass window
  # post-bypass window is hard-coded 10 h (ZendureSetpoint.POST_BYPASS_WINDOW_HOURS)
  power_target_bias_steps: 0.5         # subtract this many steps from raw target
  batt_low_stop_hysteresis_pct: 5      # latch releases only after SoC recovers by this %

zendure_hub_monitor:
  module: ZendureHubMonitor
  class: ZendureHubMonitor
  update_interval: "20min"
  mqtt_topic_write: "iot/73bkTV/SE7546CU/properties/write"
  mqtt_topic_read:  "iot/73bkTV/SE7546CU/properties/read"
  # Sparse {hour: mode}: each entry sets the mode from that hour onward
  # until the next entry; hour 0 wraps from the last defined hour
  # (cyclical day). refine_active_mode picks charge/dual-limit/dual at
  # runtime within the 'dual' slots.
  schedule:
    6:  dual    # battery contributes 06:00..14:59
    15: serve   # grid-direct 15:00..05:59 next day
  low_batt_minsoc: 100                 # 10 % * 10 (Zendure minSoc *10)
  med_batt_minsoc: 200                 # 20 % * 10
  mode_pick_low_stop_pct: 20           # SoC ≤ this → 'charge' during dual hours
  dual_limit_threshold_pct: 30         # SoC < this AND old != 'dual' → 'dual-limit'
  weekly_charge_force_hours: 174       # 7.5 d without bypass → force 'charge'
  bypass_tracker:
    debounce_seconds: 60
    solar_threshold_w: 50
    fallback_days_when_missing: 7
```

## Behaviour summary

### Setpoint (every 20 s)

1. Read consumption / solar (with fallback) / battery / mqtt-derived sensors and `zendure.operation_mode`.
2. Derive `bypass_now = (outputpackpower == 0) AND (packstate == 'idle')`.
3. Compute `hours_since_last_bypass` from `sensor.zendure_bypass_reached_at`.
4. Pick effective floor: `batt_low_stop = effective_batt_low_stop(bypass_now, hours_since_last_bypass, after_bypass=10, default=20, window_h=10)`.
5. Update `battery_discharged` latch via `battery_discharged_latch(level, batt_low_stop, hysteresis_pct, prev)`. Write `sensor.zendure_battery_discharged_shadow` only when the bool flips.
6. Raw target: `power_con − power_sol − (power_step * power_target_bias_steps)`. Quantize to `power_step`.
7. Apply mode cap:
   - `charge` → setpoint = 0.
   - `dual` → cap = `dual_cap` (720). Battery drains freely up to the cap.
   - `dual-limit` → cap = `(solarInputPower // step) * step`. Output exactly tracks solar production; battery doesn't drain.
   - default (`serve` / unknown) → cap = `serve_cap` (540).
8. Battery protection: `electric_level ≤ batt_low_stop` OR `battery_discharged` → setpoint = 0.
9. Clamp `0 ≤ setpoint ≤ cap`.
10. If changed since last publish → publish MQTT `outputLimit`. Always update `sensor.zendure_setpoint` (or `*_shadow`).

### State machine (every 20 min, aligned to clock boundaries)

1. `scheduled_mode = schedule[now.hour]`.
2. `new_mode = refine_active_mode(scheduled_mode, electric_level, old_mode, mode_pick_low_stop_pct, dual_limit_threshold_pct)`. Refines `dual` slots only:
   - `level ≤ low_stop_pct` (20 %) → `charge`.
   - `level < threshold_pct` (30 %) AND `old_mode != 'dual'` → `dual-limit`.
   - Else → `dual`.
3. `new_mode = force_weekly_charge(new_mode, hours_since_last_bypass, weekly_charge_force_hours)`. If ≥ 174 h → `charge` regardless.
4. On mode change vs `zendure.operation_mode`: publish `getAll` to read topic, then `run_in(_send_mode_payload, 5)` (non-blocking).
5. `pick_mode_payload(old, new, bypass_now, level, days_since, low_minsoc, med_minsoc)`:
   - `→ serve` + bypass → `passMode:1, minSoc:low`; advance.
   - `→ serve` + level≥30 + days<7 → `minSoc:med`; advance.
   - `→ serve` else → no payload, **don't advance**.
   - `→ dual` + level<20 + days<7 → no payload, **don't advance**.
   - `→ dual` else → no payload, advance.
   - `→ charge` → `outputLimit:0, passMode:0, minSoc:low`; advance.
   - `→ dual-limit` → no payload, advance (refinement already validated SoC).
   - same mode + bypass → `outputLimit:0, passMode:0, minSoc:low`.
6. Publish payload if non-`None`. Update `zendure.operation_mode` (or `*_shadow`).

Schedule cadence: aligned to clock boundaries via `app_helpers.next_aligned_minute` so ticks land at `:00`/`:20`/`:40` regardless of when AppDaemon restarted; `run_in(_tick, 1)` fires immediately on init so the shadow sensor populates without waiting up to a full interval.

### EnergyMeterTotals (every 5 min)

1. Iterate over `sensors` list (configurable in `apps.yaml`). If any sensor is `unknown` / `unavailable`, skip the tick silently.
2. Sum live readings and add `legacy_kwh_offset` (fixed kWh from decommissioned inverters — see `apps.yaml` comment for breakdown).
3. Write `sensor.power_meter_solar_total` (`state_class: total_increasing`, `kWh`) only when the rounded value changes.

No dry_run gate — the sensor is purely observational and has no control effect.
Ports `engery_meter_totals.py` from `zendure-solarflow-control`; legacy constants (HM-700: 7.114, original HM-1500: 134.176 → sum 141.290) moved to `apps.yaml` as `legacy_kwh_offset`.

### Bypass tracker (event-driven, hosted in `ZendureHubMonitor`)

1. `initialize()` registers `listen_state` on the four predicate inputs.
2. On any change, evaluate `is_bypass_active(electric_level, packstate, outputpackpower, solarinputpower, solar_threshold)`.
3. If True and no debounce timer pending → `run_in(_confirm_bypass, debounce_seconds)`. If False → cancel any pending timer.
4. `_confirm_bypass` re-evaluates; if still True, write `now()` (TZ-aware ISO) to `sensor.zendure_bypass_reached_at`.
5. Bootstrap on init: read existing sensor; if missing, fall back to `now − fallback_days_when_missing` and write the sensor immediately so dashboards have a value.

A separate diagnostic sensor `sensor.zendure_bypass_active` is updated on every input change AND on every `sensor.zendure_mqtt_bypass` change, exposing the 4-state agreement (`none` / `app_only` / `zendure_only` / `both`). Written only when the state flips, so HA history stays clean.

## Risk notes / parking lot

- **`time.sleep` is forbidden.** The original mode-change `getAll → wait 5 s → publish` is implemented as `run_in(_send_mode_payload, 5)`.
- **Bypass tracker may not fire in real life until conditions actually align** (battery 100 % AND idle AND outputpackpower 0 AND solar > 50 W for ≥ 60 s). Until that happens, `sensor.zendure_bypass_reached_at` stays at the 7-day fallback set on bootstrap. Once 7 days from bootstrap pass, `force_weekly_charge` (SM-20) starts firing every tick, producing a spurious `→ charge` divergence from production (which has its own `last_triggered` source). Resolves itself the first time the predicate trips for real; until then, expect the shadow to disagree with live during `dual` hours.
- **`tests/` and `tools/` require `exclude_dirs` on the HA host.** AppDaemon's hot-reload watcher tries to import every modified `.py` under `app_dir` even from subdirectories. Add to `/config/appdaemon.yaml`:
  ```yaml
  appdaemon:
    exclude_dirs:
      - tests
      - tools
  ```
  Without this, every push that touches a test or tool file emits a non-fatal but noisy import-error stack trace.
- **Half-step bias** is inherited tuning from the production script. Port verbatim, revisit only if the soak window shows persistent unexplained divergence. (The dual-mode `half_solar = solar − 60` cap was dropped in the dual_cap/serve_cap simplification — production doesn't apply it either, and dropping it actually closes a divergence rather than opening one.)
- **60 s debounce / 50 W solar threshold** for the bypass tracker are estimates. Verify against the next live bypass event; tune if it false-triggers or misses.
- **`input_select.zendure_operation_mode_strategy`** (manual override: force-serve / force-charge / etc.) is reserved for a future feature. Not wired up.
- **HA recorder `purge_keep_days = 30` (backlog).** Default is 10 days. If `sensor.zendure_bypass_reached_at` goes that long without a real bypass and HA restarts in the window, the entity can come back `unknown` and the bootstrap falls back to `now − 7 d`. Bumping to 30 days neatly closes that gap (roughly 3× DB size; consider `recorder.exclude` for the very chatty `power_consumption` / `zendure_mqtt_*` if disk pressure becomes an issue). Defer until a real bypass moment is captured first; revisit if the spurious-`charge` divergence persists.

## Implementation refinements

- **Bootstrap the bypass-timestamp on first init** so the dashboard sensor exists from t=0 (not just on the first real bypass).
- **Cold-start `zendure.operation_mode` may be `unknown` / `unavailable` / `None`.** State-machine glue treats that as same-as-`new_mode` (no transition payload, just write current). Tested via TST-21 / TST-22.
- **Shadow setpoint format must mirror live byte-for-byte.** Live writes `repr(round(setpoint, 0))` → `"30.0"`; shadow writes the same string for clean chart comparison.
- **`device_class: timestamp` requires TZ-aware ISO-8601.** Always use `self.datetime().isoformat()`; never naive `datetime.now()`.
- **Run-in kickoff (1 s after init) for both apps**, plus aligned `run_every` for the state machine. AppDaemon's `run_every(callback, "now", interval)` actually first-fires at `start + interval`, not at `start` — without the kickoff, the state-machine shadow would wait up to 20 min after every restart.
- **AppDaemon-created sensors survive HA restarts via the recorder DB**, not via YAML declaration. Existing sensors (`sensor.power_consumption` etc.) already rely on this. Risk: `purge_keep_days` (default 10) without a write can drop a sensor; mitigation if it bites is MQTT-discovery sensors, not yet needed.
