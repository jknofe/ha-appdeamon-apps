"""pytest config — puts the repo root on sys.path so tests can import app modules.

Lives inside tests/ so the apps directory at the repo root stays clean of
test-only files. AppDaemon can then exclude this whole folder via
appdaemon.yaml `exclude_dirs: [tests]` if it complains about unreferenced files.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))
