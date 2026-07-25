"""pytest config — puts the repo root on sys.path so tests can import app modules.

Lives inside tests/ so the apps directory at the repo root stays clean of
test-only files. AppDaemon can then exclude this whole folder via
appdaemon.yaml `exclude_dirs: [tests]` if it complains about unreferenced files.
"""
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


def _stub_appdaemon():
    """Make `import appdaemon.plugins.hass.hassapi` work without AppDaemon.

    AppDaemon is deliberately never installed on the dev machine (see
    WORKING-STYLE.md) - integration is verified on the HA host instead. The pure
    functions under test live at the top of the app modules, but importing those
    modules pulls in the AppDaemon base class, so we register a stub package
    first. Only the `Hass` name is needed, and only at class-definition time; no
    test touches AppDaemon behaviour.
    """
    if "appdaemon.plugins.hass.hassapi" in sys.modules:
        return
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        mod = types.ModuleType(name)
        mod.__path__ = []
        sys.modules[name] = mod
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        pass

    hassapi.Hass = Hass
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi
    sys.modules["appdaemon.plugins.hass"].hassapi = hassapi


_stub_appdaemon()
