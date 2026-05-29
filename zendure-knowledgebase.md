# Zendure SolarFlow AppDaemon Knowledgebase

Reference for the AppDaemon lean rewrite of the Zendure SolarFlow control logic.
Captures *why* the apps look the way they do. Day-to-day mechanics live in
`zendure-requirements.md` (testable spec) and the source files themselves.

**Migration status: complete.** All legacy `python_script.*` automations and
scripts have been removed from HA. AppDaemon apps are the sole control path.

## Goal

Maximize solar self-consumption: only what the home cannot use right now goes into
storage. The control law is `outputLimit = max(0, consumption - solar_secondary)`,
quantized and capped by mode. Keep HA-visible surface (sensors, dashboards) unchanged.
No new credentials -- reuse HA's MQTT integration.

## Scope

In scope:
- `ZendureSetpoint` AppDaemon app (every 20 s; replaces `python_script.zendure_setpoint`).
- `ZendureHubMonitor` AppDaemon app (event-driven bypass tracker + one-time firmware init;
  replaces `python_script.zendure_state_machine`, `automation.zendure_bypass_reached`,
  and the `automation.zendure_charge_start` daily minSoc reset).
- `EnergyMeterTotals` AppDaemon app (every 5 min; ports `engery_meter_totals.py`).

Out of scope:
- `zendure_state_decoder.py` -- already redundant; HA YAML decodes `packState`.
- `zendure_battery_state.py` -- stub.
- `power_*.py` -- superseded by `PowerMeter.py`.
- `configuration_mqtt_sensors.yaml` -- moved to `/config/zendure_mqtt_sensors.yaml`;
  included via `mqtt: !include zendure_mqtt_sensors.yaml`. AppDaemon reads the resulting
  `sensor.zendure_mqtt_*` entities.
- `input_select.zendure_operation_mode_strategy` -- future manual-override feature.

## Design decisions

| # | Decision | Why |
|---|---|---|
| Q1 | Two control apps: `ZendureSetpoint` (every 20 s) and `ZendureHubMonitor` (event-driven). Decoder dropped. | Cadences differ wildly; merging would complicate both. |
| Q2 | MQTT publish via HA service `mqtt/publish`. | No broker creds needed in AppDaemon. |
| Q3 | MQTT subscribe / decoding stays in HA YAML; AppDaemon reads `sensor.zendure_mqtt_*`. | Lowest-risk slice; HA YAML already does it reliably. |
| Q4 | Persistent flags stay as HA sensor entities. | Visible on dashboards, restored after HA restart via recorder DB. |
| Q5 | `dry_run` in `apps.yaml` only -- no `input_boolean` HA helper. Default `true`. | A dashboard toggle can be flipped by accident. `apps.yaml` requires a deliberate edit + AppDaemon reload. Also hot-reload on `apps.yaml` edits makes the toggle instant without a helper. |
| Q6 | Shadow mode: with `dry_run=true` all MQTT publishes go to `shadow/<topic>` and HA writes go to `*_shadow` sensors. Same payload, different destination. Flip `dry_run=false` on both apps to go live. | Inverter is driven every 20 s; a bug means wrong power flow. Shadow lets us observe vs. the legacy script on the live topic while the new apps write to shadow. |
| Q7 | Bypass tracker inside `ZendureHubMonitor`, `listen_state` + 60 s symmetric debounce on `level==100 AND packstate=='idle' AND outputpackpower==0 AND solarinputpower>50 W`. In-memory `_bypass_active` latch: one INFO log + timestamp write on confirmed start, one on confirmed end, silent in between. | Replaces the unreliable `sensor.zendure_mqtt_bypass` (Zendure's self-reported `pass` flag is delayed and sometimes wrong) and the dumb "battery 100 % long enough" HA automation. Keeping it inside `ZendureHubMonitor` avoids a third app. The latch was added after observing ~150 `Bypass reached at` log lines during a single 3 h bypass on 2026-05-22 -- without it, every input-sensor tick during the bypass started a new debounce that re-confirmed and re-logged. |
| Q8 | Three modes: `free`, `solar-only`, `charge`. No schedule, no hour-of-day logic. | The old 4-mode schedule (`dual`/`serve`/`dual-limit`/`charge` with a `{hour: mode}` config) was over-fitted to a specific day shape. The lean rewrite derives mode purely from SoC + solar + bypass recency -- it adapts to weather without any calendar config. |
| Q9 | `charge_latch` with `LATCH_HYSTERESIS_PCT = 5 %`. Once SoC <= floor, latch sticks; releases only at SoC >= floor + 5 %. | A 1 % SoC bounce was flapping discharge on/off on the old system. Hysteresis avoids the chatter without trapping us at low SoC permanently. |
| Q10 | `free_latch` -- daily drain commitment. Engages when SoC >= `soc_promote` (default 30 %); cleared when `charge_latch` engages. | Stops a transient mid-day SoC dip from yanking us back to `solar-only` and stranding stored energy. Once we've committed to draining for the day, we stay in `free` through minor dips. |
| Q11 | Discharge floor is **dynamic**, picked each tick by `effective_floor`: 10 % when `hours_since_bypass < 10 h`, else 20 %. | Freshly-charged battery is safe to drain deeper. No recent bypass means keep a 20 % reserve. Re-evaluating per tick avoids sticky-state bookkeeping. |
| Q12 | 174 h (7.5 d) without a confirmed bypass force-overrides mode to `charge`. | Ensures a weekly full-cycle in winter / multi-day overcast. |
| Q13 | `solar-only` mode: cap = `quantize(solar_input_power)`. Battery preserved; output exactly tracks the hub's DC solar production. | Analogous to the old `dual-limit` mode but simpler: no SoC threshold or anti-bounce condition. Fires at mid-SoC under real sun when SoC has not yet reached `soc_promote`. |
| Q14 | `sensor.zendure_mqtt_outputhomepower` (Zendure-reported DC feed to HM-1500, ~5 % optimistic vs actual HM-1500 AC) is used as `solar_primary_power` for observability only -- not in the control law. | **(a) Reliability** -- Zendure MQTT keeps streaming when OpenDTU freezes. **(b) Update cadence** -- Zendure reports much faster. **(c) Not used in the control law** -- setpoint equation uses `consumption - solar_secondary` (HM-400 only), never reads HM-1500 back. The ~5 % DC-vs-AC gap does not propagate anywhere. |
| Q15 | `sensor.zendure_bypass_reached_at` is re-read each tick, not cached at boot. `_hours_since_last_bypass` handles TZ-mismatch (naive vs aware) by coercing tzinfo. | Original code parsed the sensor once at bootstrap. If the sensor was missing, the cache was set to `now - 7 d`; roughly 7 h later `force_weekly_charge` fired and kept firing every tick because nothing updated the cache. Re-reading each tick picks up any external write immediately. |
| Q16 | Pure functions (`effective_floor`, `update_charge_latch`, `pick_mode`, `compute_setpoint`, `is_bypass_active`, `bypass_status`) defined inline in their respective app files. No separate `zendure_logic.py` module. | The lean rewrite has so few pure functions (six total, short bodies) that a separate module adds indirection with no benefit. Tests import from the app module directly. |

## MQTT topics

- **Read** (decoded by HA YAML): `/73bkTV/SE7546CU/properties/report`
- **Write** (published via `mqtt/publish` service):
  - `iot/73bkTV/SE7546CU/properties/write` -- setpoint: `{"properties": {"outputLimit": <int>}}`;
    firmware init: `{"properties": {"minSoc": <int>, "passMode": <int>, "outputLimit": 0}}`.

In dry_run mode all publishes go to `shadow/iot/73bkTV/SE7546CU/properties/write` instead.

## HA entities consumed

**ZendureSetpoint inputs (each tick)**
- `sensor.power_consumption` -- produced by `PowerMeter.py`
- `sensor.hm_400_power` -- uncontrolled solar (HM-400 AC); subtracted from demand
- `sensor.zendure_mqtt_solarinputpower` -- DC into Zendure hub; drives `solar-only` cap
- `sensor.zendure_mqtt_electriclevel` -- battery SoC
- `sensor.zendure_bypass_reached_at` -- re-read each tick; drives floor + weekly force

**ZendureHubMonitor inputs (event-driven)**
- `sensor.zendure_mqtt_electriclevel`, `sensor.zendure_mqtt_packstate`,
  `sensor.zendure_mqtt_outputpackpower`, `sensor.zendure_mqtt_solarinputpower` -- bypass predicate inputs
- `sensor.zendure_mqtt_bypass` -- Zendure's reported `pass` flag (for BT-7 diagnostic only)

## HA entities written

**Always (not gated by dry_run)**
- `sensor.zendure_bypass_reached_at` (`device_class: timestamp`) -- bypass tracker.
- `sensor.zendure_bypass_active` (4-state: `none` / `app_only` / `zendure_only` / `both`) -- diagnostic.

**Shadow mode (dry_run=true)**
- `sensor.zendure_setpoint_shadow`
- `sensor.zendure_operation_mode_shadow`
- `sensor.zendure_battery_discharged_shadow` (`"True"` / `"False"` string)

**Live mode (dry_run=false)**
- `sensor.zendure_setpoint`
- `sensor.zendure_operation_mode`
- `sensor.zendure_battery_discharged`

## `apps.yaml` config

```yaml
zendure_setpoint:
  module: ZendureSetpoint
  class: ZendureSetpoint
  update_interval: "20s"
  mqtt_topic_write: "iot/73bkTV/SE7546CU/properties/write"
  dry_run: false
  power_inputs: &power_inputs
    power_consumption:     "sensor.power_consumption"
    solar_primary_power:   "sensor.zendure_mqtt_outputhomepower"
    solar_secondary_power: "sensor.hm_400_power"
    solar_input_power:     "sensor.zendure_mqtt_solarinputpower"
  max_cap: 720                         # W - outputLimit cap in 'free' mode
  power_step: 30                       # W - quantization step
  power_target_bias_steps: 0.5         # half-step under-supply bias
  batt_floor_after_bypass: 10          # % - discharge floor inside 10 h post-bypass window
  batt_floor_default: 20               # % - discharge floor outside the post-bypass window
  soc_promote_to_free: 30              # % - SoC at/above which we commit to drain for the cycle
  solar_threshold_w: 100               # W - solar input above this = real daytime
  weekly_charge_force_hours: 174       # h - without confirmed bypass -> force 'charge'

zendure_hub_monitor:
  module: ZendureHubMonitor
  class: ZendureHubMonitor
  mqtt_topic_write: "iot/73bkTV/SE7546CU/properties/write"
  dry_run: false
  power_inputs: *power_inputs
  bypass_tracker:
    debounce_seconds: 60
    solar_threshold_w: 50
    fallback_days_when_missing: 7
  firmware_init:
    min_soc: 10                        # % - app multiplies x10 before sending to Zendure
    pass_mode: 0                       # 0 = normal operation
```

## Deployment

App dir is a git checkout at `/root/addon_configs/a0d7b954_appdaemon/apps` on the
HA host. The AppDaemon add-on bind-mounts `/root/addon_configs/a0d7b954_appdaemon/`
into the container as `/config/`, so AppDaemon sees the same files at
`/config/apps` and reads `/config/appdaemon.yaml`. Deploys are `git pull` at the
host path; there is no build or sync step.

**VERSION marker.** Each app logs `<AppName> started (version: <VERSION>)` at the
top of `initialize()`. Bump the `VERSION` constant in the `.py` before each
deploy, then `ha apps logs a0d7b954_appdaemon | grep "started (version:"` to
confirm the new file actually loaded. Added 2026-05-26 after a deploy reached
the right directory but the running AppDaemon process never picked up the new
code.

**Structural `apps.yaml` changes need a full add-on restart, not a hot-reload.**
AppDaemon's watcher picks up content changes to existing modules but does NOT
cleanly handle adding or removing an `apps.yaml` key: the old app instance keeps
running on its in-memory bytecode, and a newly-added key (if any) gets started
in parallel. Observed 2026-05-29 after the `zendure_state_machine` ->
`zendure_hub_monitor` rename in 44921fd: both apps ran simultaneously for days,
with the old one still spamming `Bypass reached at`. Fix:
`ha apps restart a0d7b954_appdaemon`, which fully tears both down, re-parses
`apps.yaml`, and starts the new set.

**Auth uses `SUPERVISOR_TOKEN`; never hardcode a long-lived HA token in
`appdaemon.yaml`.** Supervisor injects `SUPERVISOR_TOKEN` into the add-on
container at every start; it is short-lived (rotates per add-on restart) and
scoped to the add-on manifest. AppDaemon's HASS plugin reads it via
`token: !env_var SUPERVISOR_TOKEN`. A hardcoded long-lived token was found in a
redundant top-level `plugins:` block in `appdaemon.yaml` and removed
2026-05-29 (revoke any such token in HA -> user profile -> Long-Lived Access
Tokens whenever one shows up here).

## Behaviour summary

### ZendureSetpoint (every 20 s)

1. Read SoC, consumption, solar_secondary (HM-400), solar_input (Zendure DC), hours_since_bypass.
2. Compute effective floor (`effective_floor`): 10 % if bypass was < 10 h ago, else 20 %.
3. Update charge_latch (`update_charge_latch`): engages at SoC <= floor, releases at SoC >= floor + 5 %. On engage, clear free_latch and write `zendure_battery_discharged` sensor.
4. Pick mode (`pick_mode`): weekly force -> charge_latch -> free_latch -> soc_promote -> solar -> fallback. Returns mode + updated free_latch + reason string.
5. Compute setpoint (`compute_setpoint`): target = consumption - solar_secondary - bias. Quantize. Cap by mode. Clamp >= 0.
6. Write setpoint sensor (skipped if unchanged). Write mode sensor (skipped if unchanged). Publish MQTT outputLimit (skipped if unchanged since last publish).
7. Log mode transitions at INFO (`Mode <old> -> <new>: <reason>`). Silent otherwise.

### ZendureHubMonitor

**Bypass tracker** (event-driven, latched state machine):
1. `initialize()` bootstraps `sensor.zendure_bypass_reached_at` if missing/unparseable. Initializes `_bypass_active = False`. After wiring listeners, kicks the state-machine entry once to catch a bypass already in progress at startup.
2. `listen_state` on four predicate inputs. On any change: evaluate `is_bypass_active`. If result disagrees with `_bypass_active` and no timer pending -> start 60 s `_confirm_transition` timer. If result agrees with `_bypass_active` (predicate flapped back to latched state) and timer pending -> cancel.
3. After 60 s debounce, `_confirm_transition` re-evaluates and acts on the transition direction:
   - latch False -> True: write `now()` to `sensor.zendure_bypass_reached_at`, log INFO `Bypass started at <iso>`
   - latch True -> False: log INFO `Bypass ended at <iso>`. NO timestamp write -- the sensor advances exactly once per bypass cycle, on the start
   - predicate flipped back during debounce: no log, no write
4. Maintain `sensor.zendure_bypass_active` (4-state diagnostic) on every predicate-input or `sensor.zendure_mqtt_bypass` change. Write only on flip.

**Why the latch / why two logs but one timestamp write** -- the previous implementation cleared `_pending_handle` inside `_confirm_bypass` without latching, so any subsequent input change while the predicate was still True started a fresh 60 s timer, re-confirmed, re-logged INFO, AND re-wrote `sensor.zendure_bypass_reached_at`. Long sunny bypasses produced one INFO + one recorder write every ~60 s for hours (e.g. ~150 lines / 150 timestamp updates over a 3 h bypass on 2026-05-22). The latch makes "in bypass" a sticky state: one start log + one timestamp write when we enter, one end log when we leave, silent in between. The timestamp is intentionally NOT rewritten on the end transition so the sensor advances once per cycle, matching its literal name ("reached at") and avoiding recorder churn.

**Firmware init** (once, 5 s after start):
- Publish `{minSoc: 100, passMode: 0, outputLimit: 0}` to the write topic (or `shadow/...` in dry_run). Sets the firmware hard floor at 10 %; `ZendureSetpoint` enforces the higher soft floor via `outputLimit`.

### EnergyMeterTotals (every 5 min)

1. Iterate `sensors` list. If any value unavailable -> skip tick silently.
2. Sum live kWh + `legacy_kwh_offset` (HM-700: 7.114 kWh + original HM-1500: 134.176 kWh = 141.290 kWh).
3. Write `sensor.power_meter_solar_total` only if rounded value changed.

## HA configuration state (post-migration)

### What was removed (2026-05-12)

All legacy control scripts and their triggering automations were deleted from the HA host.
Backups are in `/config/.backup_conf/`.

**`/config/python_scripts/` - deleted entirely.** Contained:
- `zendure_setpoint.py`, `zendure_state_machine.py` - replaced by AppDaemon apps
- `power_import_export.py`, `power_consumption.py`, `power_solargen.py` - replaced by `PowerMeter`
- `engery_meter_totals.py` - replaced by `EnergyMeterTotals`
- `zendure_state_decoder.py`, `zendure_battery_state.py`, `get_state.py` - dead stubs
- `automations.yaml` - all 8 entries deleted (see below)

**Automations removed from `python_scripts/automations.yaml` (whole file deleted):**
`Zendure Setpoint Update`, `Power_Import_Export_Python`, `Power_Consumption_Python`,
`Power_Solargen_Python`, `Engery_Meter_Totals`, `Zendure Charge Start`,
`Zendure System State Machine`, `Zendure Bypass Reached`

**Automation removed from `automations.yaml`:**
`Zendure Low Stop` (id 1715005016756) - empty action, was always a no-op.

**Lines removed from `configuration.yaml`:**
- `python_script:` - integration no longer needed
- `automation split: !include python_scripts/automations.yaml` - file is gone

### What moved

`python_scripts/configuration_mqtt_sensors.yaml` -> `/config/zendure_mqtt_sensors.yaml`.
Include path in `configuration.yaml` updated to `mqtt: !include zendure_mqtt_sensors.yaml`.
This file defines all `sensor.zendure_mqtt_*` entities that AppDaemon reads -- keep it.

## Risk notes / parking lot

- **`time.sleep` is forbidden.** The firmware init `5 s delay` is implemented as `run_in(_send_firmware_init, 5)`.
- **Bypass tracker may not fire until conditions align** (battery 100 % AND idle AND outputpackpower 0 AND solar > 50 W for >= 60 s). Until then, `sensor.zendure_bypass_reached_at` stays at the bootstrap fallback (7 d ago). Once 174 h from bootstrap pass without a confirmed bypass, `weekly_charge_force_hours` fires and keeps the system in `charge` mode until the next real bypass. Resolves itself the first time the predicate trips for real.
- **`tests/` and `tools/` require `exclude_dirs` in `/config/appdaemon.yaml`.** AppDaemon's hot-reload watcher imports every modified `.py` under `app_dir` including subdirectories. Without `exclude_dirs: [tests, tools]`, a push that touches a test file emits a non-fatal but noisy import-error stack trace.
- **Half-step bias (`power_target_bias_steps: 0.5`)** is inherited tuning from the legacy script. Revisit only if the soak window shows persistent unexplained grid-import bias beyond the HM-1500 inverter physics loss (~5 % DC-to-AC).
- **60 s debounce / 50 W solar threshold** for the bypass tracker are estimates. Tune after the next live bypass event if it false-triggers or misses.
- **`input_select.zendure_operation_mode_strategy`** (manual override: force-serve / force-charge / etc.) is reserved for a future feature. Not wired up.
- **HA recorder `purge_keep_days = 30` (backlog).** Default is 10 days. If `sensor.zendure_bypass_reached_at` goes that long without a real bypass and HA restarts in the window, the entity can come back `unknown` and the bootstrap falls back to `now - 7 d`. Bumping to 30 days closes that gap. Defer until a real bypass is captured; revisit if spurious `charge` mode recurs.
- **`free_latch` not persisted.** After a restart, the first tick may pick `solar-only` instead of `free` if SoC is between floor and `soc_promote`. Acceptable -- the next tick that re-evaluates will promote to `free` once `soc_promote` is met.

## Implementation notes

- **Bootstrap charge_latch on init** from `sensor.zendure_battery_discharged` (or shadow fallback) so a restart mid-discharge does not briefly re-enable drain.
- **`device_class: timestamp` requires TZ-aware ISO-8601.** Always use `self.datetime().isoformat()`; never naive `datetime.now()`.
- **Shadow setpoint format must mirror live byte-for-byte.** Both write `repr(round(setpoint, 0))` -> e.g. `"30.0"`. Same string for clean chart comparison.
- **AppDaemon-created sensors survive HA restarts via the recorder DB**, not via YAML declaration. Risk: `purge_keep_days` (default 10) without a write can drop a sensor. Mitigation if it bites: MQTT-discovery sensors -- not yet needed.
- **TZ coercion in `_hours_since_last_bypass`:** if the stored timestamp is naive and `self.datetime()` is aware (or vice versa), the subtraction would raise. The implementation coerces tzinfo to match before subtracting.
