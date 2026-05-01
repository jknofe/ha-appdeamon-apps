# Working Style

Conventions for changes in this repo. Solo dev, direct to `main`, no PRs.

## Git

- **Branching**: direct to `main`. No feature branches.
- **Commit cadence**: one commit per task box in any in-progress `*-tasks.md` (or per other small logical unit). Each commit must leave AppDaemon loadable — no half-edited apps.
- **Tick boxes** in the same commit that does the work.
- **Push**: never automatically. The user pushes manually. Do not run `git push` unless explicitly asked.
- **Subject style**: imperative mood, capital first letter, no period, no `feat:`/`fix:` prefix, name the class when relevant. Examples:
  ```
  Add ZendureStateMachine skeleton with shadow-mode outputs
  Port operation-mode schedule into ZendureStateMachine
  Gate Zendure MQTT publish on input_boolean.zendure_dry_run
  Tick Phase 2 boxes in zendure-tasks.md
  ```
- **Body**: usually skip. Add only if the *why* is non-obvious from the diff.

## Python

- Match `PowerMeter.py` style: 4-space indent, no type hints, light docstrings on methods, sparse comments.
- Keep code clean and simple. Prefer flat structure over abstraction layers.
- No new tooling (no `ruff`, `black`, `pyproject.toml`). Two apps don't justify it.

## Comments

- Comment **main flow decisions** — the *why* behind a non-obvious branch.
  Examples worth a comment: "latch+hysteresis on `battery_discharged` so a 1 % SoC bounce doesn't re-enable discharge", "5 s wait after mode-change MQTT to let Zendure reply with current state", "dual-limit caps inverter to current solar input so we never feed-in from battery".
- Don't narrate what the code does. Skip redundant comments like `# read state` above a `self.get_state(...)` call.

## Reload safety (AppDaemon-specific)

- AppDaemon hot-reloads a `.py` file on save and re-runs `initialize()`. Implications:
  - `initialize()` must be cheap and idempotent. No long blocking work, no side effects you wouldn't want repeated.
  - Periodic callbacks (`run_every`, `run_every_minute`) should guard against in-flight reentry with an `_is_running` flag (see `PowerMeter.py:33`).

## Testing — two layers

### Layer 1: local `pytest` (fast feedback, no AppDaemon)

- Pure logic lives in module-level functions in **`zendure_logic.py`**. No `self.*`, no AppDaemon imports.
- Tests live in **`tests/`** and import only `zendure_logic`.
- On the Mac: `pip install pytest`, then `pytest` in repo root. AppDaemon is *not* installed locally.
- Cover the math: setpoint quantization, mode caps (`charge` / `dual-limit` / `dual` / bypass), battery-discharged latch + hysteresis, weekly-bypass override, hour-schedule pick.
- Iterate: edit logic → save → `pytest` → seconds.

### Layer 2: shadow mode on HA (integration check, real data)

- AppDaemon apps load on the HA host with `input_boolean.zendure_dry_run = on`.
- Apps compute everything but write only to `*_shadow` sensors. No MQTT publish to Zendure. Existing python_scripts continue to drive the inverter.
- Verify by graphing live vs shadow sensors for ≥ 24 h. Reconcile every persistent diff.
- Cutover: flip `dry_run` off, disable HA python_script automations.

## File layout

```
ha-appdeamon-apps/
├── apps.yaml                       # AppDaemon manifest
├── PowerMeter.py / Hello.py        # existing
├── ZendureSetpoint.py              # AppDaemon glue
├── ZendureStateMachine.py          # AppDaemon glue
├── zendure_logic.py                # pure functions, no AppDaemon imports
├── tests/
│   ├── conftest.py
│   └── test_zendure_logic.py
├── .venv/                          # local pytest env (gitignored, AppDaemon-ignored as dotfile)
├── conftest at tests/conftest.py
├── *.md (design docs)
└── .gitignore
```

### Why this layout is AppDaemon-correct

- AppDaemon only imports files referenced from `apps.yaml` containing `hass.Hass` subclasses; everything else is invisible to the app loader. So `zendure_logic.py` and `tests/` cause no app-loading interference.
- The official docs explicitly bless the shared-library-at-top-level pattern: *"Python modules may be imported directly if they are in a directory in which other apps reside."* AppDaemon tracks dependencies via AST so apps reload when shared modules change.
- AppDaemon auto-ignores any path containing a `.` segment, so `.venv/` is invisible. `tests/` (no dot) is not auto-ignored, but it's also not imported (no entry in `apps.yaml`). If cosmetic warnings ever appear, add `exclude_dirs: [tests]` to `appdaemon.yaml` on the HA host.
- A `tests/` *with* leading dot would be hidden from AppDaemon AND from pytest's default `norecursedirs`, so we keep the no-dot name.
- We do not group Zendure files into a `zendure/` package: it would force `apps.yaml` to use `module: zendure.ZendureSetpoint` and break consistency with the flat `PowerMeter.py`.

## Logging

- Match `PowerMeter.py`: `self.log(...)` sparingly, mostly on state changes or unexpected branches; explicit log on exceptions.
- Don't log every cycle. The state-machine (20 min) can be chattier than the setpoint (20 s).
- Use levels: default INFO for state changes, WARNING for guard skips, ERROR for caught exceptions.
