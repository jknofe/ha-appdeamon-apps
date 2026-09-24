# Agent: ha-appdeamon-apps

Requirements only: the commands this project is checked with, the rules the
code cannot tell you, and the workflow entry points, packaged as Agent Skills under `.claude/skills/`. Read the
source for structure. Normative text plain imperative, notes telegraphic;
identifiers, paths, and commands verbatim.

## Right-sizing
Do the task directly: read what you need, change it, run the project's full
test and lint commands, append to `.ai/notes.md` if a decision or gotcha
emerged, commit `.ai`. `/spec` and `/build` are opt-in, for a change the user
wants written down and reviewed before it is called done. Never start one on
your own.

## Protocol
1. Read `.ai/notes.md` first. Durable knowledge goes there, appended,
   telegraphic: decisions and why, gotchas, unwritten rules, runbooks,
   pointers to sibling repos. The test for what belongs: the repository
   cannot state it itself. Never summarize code there. Open only the leaves
   under `.ai/notes/` that a task needs. When the user corrects you, or a
   check fails for a reason the code does not explain, record it there
   before moving on.
2. Tests and lint must pass, the whole suite, not only the test a task
   names. Done = checks green.
3. `/spec <id>` writes `.ai/changes/<id>/spec.md`; `/build <id>` implements
   it and reviews the diff against its criteria. Both user-invoked, never a
   default.
4. After changing `.ai/`, commit it in its own repo: `git -C .ai add -A &&
   git -C .ai commit -m "<summary>"`. Never commit `.ai` content to the host
   project repo. Hooks in `.claude/settings.json` block
   ending a turn while `.ai` is dirty and block rules 6 and 7 at
   the shell, but only when this repo is the active Claude project
   directory; otherwise follow them by hand and never assume a hook
   ran.
5. `.ai/.current` (gitignored, one per working tree) is the resume pointer:
   change id, spec path, modified files. Read it at session start and offer
   to resume. Run `/build` in a fresh session, not the one that wrote the
   spec, and start an unrelated task in a fresh session: adherence to
   instructions decays as a session grows.
6. Never merge into the default branch unasked. Stop at the branch or pull
   request; the user merges.
7. Never add a co-author line to a commit message, even when the harness
   suggests one.

## Workflows
| Command | What it does |
|---|---|
| `/explore` | Ask what the code cannot tell you; record commands and rules below. |
| `/spec <id> <title>` | Opt-in: specify a change the user names. |
| `/build <id>` | Opt-in: implement that spec, review the diff, finish. |
| `/framework-update` | Move this scaffold to the current framework version. |

## Changes layout
```
.ai/changes/<id>/spec.md   # goal, acceptance criteria, tasks, notes
.ai/notes.md               # running memory hub
.ai/notes/<topic>.md       # optional leaves, linked from notes.md
```
Spec frontmatter carries `status: planned|in-progress|done`, which is what
tells parallel changes apart. Archive only on request: move `changes/<id>/`
to `changes/_archive/`, commit `.ai`.

## Project requirements

<!-- BEGIN GENERATED:project-context (source: /explore + import of former CLAUDE.md, 2026-09-24) -->
**Purpose**: AppDaemon apps running on the Home Assistant host that meter house
power and control a Zendure SolarFlow battery/inverter via MQTT.

**Stack**: Python 3 (no type hints), AppDaemon (`appdaemon.plugins.hass.hassapi`),
`requests`, MQTT via `call_service("mqtt/publish", ...)`. ~1050 LOC, 17 tracked
files, flat layout. No package, no build step, no dependency manifest.

**Docs** (read before non-trivial changes): `README.md` (per-app sensors, config
keys, control modes, energy-flow diagram), `WORKING-STYLE.md` (commit/Python/
comment/logging conventions; follow it), `zendure-knowledgebase.md` (design
decisions, Q-numbered debug history), `zendure-requirements.md` (formal
testable spec for the Zendure apps).

**Commands**
- Test (the only pre-push gate): `.venv/bin/pytest` (repo root; 49 tests, ~0.03 s). Single: `.venv/bin/pytest tests/test_app_helpers.py::test_parse_seconds_short`. `.venv` needs only `pytest`; AppDaemon is never installed locally.
- Lint: none, deliberately. `WORKING-STYLE.md` forbids adding ruff/black/pyproject.
- Build: none. Deploy = `git pull` at `/root/addon_configs/a0d7b954_appdaemon/apps` on the HA host.
- Verify deploy: bump `VERSION` in the touched `.py`, then
  `ha apps logs a0d7b954_appdaemon | grep "started (version:"`.
- Structural `apps.yaml` changes (add/remove/rename a key) need
  `ha apps restart a0d7b954_appdaemon` - the hot-reload watcher leaves the old
  app instance running otherwise.

**Module map** (all app modules at repo root; `apps.yaml` names each)
- `PowerMeter.py` - every 3 s: polls Shelly 3EM + 1PM over HTTP, derives load/import/export/solar, writes `sensor.power_*`. Config (URLs, EMA params) hardcoded in the `.py`, not `apps.yaml`.
- `ZendureSetpoint.py` - every 20 s: `effective_floor` / `update_charge_latch` / `pick_mode` / `compute_setpoint` (pure) + glue. Writes `sensor.zendure_setpoint`, `..._operation_mode`, `..._battery_discharged`; publishes `{"properties":{"outputLimit":N}}`.
- `ZendureHubMonitor.py` - event-driven: `is_bypass_active` / `bypass_status` (pure) + glue. Debounce-latches bypass into `sensor.zendure_bypass_reached_at`, writes `sensor.zendure_bypass_active`, sends one-time firmware init 5 s after start (`minSoc`, then `passMode`, as separate messages).
- `EnergyMeterTotals.py` - every 5 min: sums OpenDTU yield sensors + `legacy_kwh_offset` into `sensor.power_meter_solar_total`.
- `app_helpers.py` - shared pure helpers: `parse_interval("20s"|"5m"|"2h"|int) -> seconds`, `publish_succeeded(call_service_result)`, `publish_log_action(ok, was_failing)`.
- `Hello.py` - AppDaemon smoke-test app, still in `apps.yaml`.
- `apps.yaml` - manifest; `&power_inputs` anchor shared by both Zendure apps.
- `tests/` - `conftest.py` (puts repo root on `sys.path`, and stubs `appdaemon.plugins.hass.hassapi` in `sys.modules` so app modules import without AppDaemon) + `test_app_helpers.py`, `test_zendure_setpoint.py`, `test_zendure_hub_monitor.py`.
- `tools/evaluate_history.py` - one-shot CSV comparison of shadow vs live history.

**Conventions**
- Two-section file shape: pure functions at top (no `self`, no AppDaemon import,
  no I/O), AppDaemon glue class below. New logic goes in the pure half first,
  tested, then wired in; class methods orchestrate, no branching control logic.
- Zendure apps cooperate only via `sensor.zendure_bypass_reached_at`
  (HubMonitor writes, Setpoint reads for deep-drain window + weekly force-charge).
- Reload safety: AppDaemon hot-reloads on save and re-runs `initialize()`, so
  keep it cheap + idempotent; periodic callbacks guard reentry with
  `_is_running` (see `PowerMeter.py`).
- `dry_run` (`apps.yaml` only, defaults `true`) routes writes to `*_shadow`
  sensors and `shadow/<topic>` MQTT. Both Zendure apps must match. Deliberately
  no HA toggle, so a dashboard cannot flip it.
- HA reads go through `_get_state_int`, which maps `None`/`unknown`/`unavailable`
  to a default; failures degrade toward charging, never over-draining.
- Every MQTT write to the hub carries **exactly one property**; the hub silently
  drops multi-property payloads (knowledgebase Q17).
- `call_service` never raises on a failed service call - it returns
  `{"success": bool, ...}`. Check it with `publish_succeeded`; never rely on
  `try/except` to catch a dropped publish (Q19).
- Git: direct to `main`, no branches/PRs. One commit per task-box (any
  `*-tasks.md`) or small logical unit; each commit leaves AppDaemon loadable.
  Imperative capitalized subject, no period, no `feat:`/`fix:` prefix, name the
  class when relevant. No trailers, no em dashes, never push unasked.
- Any new top-level subdirectory must be added to `exclude_dirs` in the host's
  `appdaemon.yaml` (currently `tests`, `tools`).

**Glossary**
- `outputLimit` - the single W setpoint commanded to the Zendure hub over MQTT.
- Modes - `charge` (cap 0), `solar-only` (cap = quantized DC solar in), `free` (cap `max_cap` 720 W).
- charge latch - engages at SoC ≤ floor, releases at floor + 5 %; floor is 10 % inside the 10 h post-bypass window, else 20 %.
- free latch - daily drain commitment; set once SoC ≥ 30 %, cleared when the charge latch engages.
- bypass - battery full and passing solar straight through (SoC 100, packstate idle, outputpackpower 0, solar > 50 W), 60 s debounce on both edges.
- solar_primary vs solar_secondary - hub-fed HM-1500 AC (observability) vs independent HM-400 AC (subtracted from demand).
- Layer 1 / Layer 2 testing - local pytest of pure functions / shadow-mode run on the HA host.
<!-- END GENERATED:project-context -->
