#!/usr/bin/env python3
"""Compare Zendure shadow vs live history exported from HA.

Usage:
    python3 tools/evaluate_history.py /path/to/history.csv

Expects a CSV with header `entity_id,state,last_changed` and at minimum:
    sensor.zendure_setpoint  /  sensor.zendure_setpoint_shadow
    zendure.operation_mode   /  sensor.zendure_operation_mode_shadow
    sensor.zendure_bypass_active
    sensor.zendure_bypass_reached_at
The setpoint diff is the headline metric (TST-INT-4: shadow within
+/- power_step of live). Inputs are summarised so divergences can be
explained without re-loading the CSV by hand.
"""
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime


POWER_STEP = 30  # apps.yaml zendure_setpoint.power_step
PAIR_TOLERANCE_SECONDS = 15


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_csv(path):
    by_entity = defaultdict(list)
    with open(path) as fh:
        for r in csv.DictReader(fh):
            by_entity[r["entity_id"]].append((parse_ts(r["last_changed"]), r["state"]))
    for rows in by_entity.values():
        rows.sort()
    return by_entity


def pair_within(a_rows, b_rows, tol_seconds=PAIR_TOLERANCE_SECONDS):
    """For each row in `a_rows`, find the closest row in `b_rows` within tol_seconds."""
    if not a_rows or not b_rows:
        return []
    out = []
    for ts, av in a_rows:
        best = min(b_rows, key=lambda x: abs((x[0] - ts).total_seconds()))
        if abs((best[0] - ts).total_seconds()) <= tol_seconds:
            out.append((ts, av, best[1]))
    return out


def section(title):
    print(f"\n=== {title} ===")


def print_overview(by_entity):
    section("Entity counts")
    for e in sorted(by_entity):
        rows = by_entity[e]
        first, last = rows[0][0], rows[-1][0]
        span_h = (last - first).total_seconds() / 3600
        print(f"  {e:50s} n={len(rows):5d}  "
              f"{first.strftime('%H:%M')}..{last.strftime('%H:%M')} "
              f"({span_h:5.1f}h)")


def print_setpoint_diff(by_entity):
    shadow = by_entity.get("sensor.zendure_setpoint_shadow", [])
    live = by_entity.get("sensor.zendure_setpoint", [])
    section("Setpoint shadow vs live (paired +/- 15s)")
    if not shadow or not live:
        print("  missing data — need both sensor.zendure_setpoint and *_shadow")
        return
    paired = pair_within(shadow, live)
    diffs = [(ts, int(s), int(l), int(s) - int(l)) for ts, s, l in paired]
    print(f"  shadow rows={len(shadow)}, paired={len(diffs)}")
    buckets = Counter(d for *_, d in diffs)
    for k in sorted(buckets):
        bar = "#" * min(50, buckets[k])
        print(f"  diff {k:+5d} W: {buckets[k]:4d}  {bar}")
    outliers = [row for row in diffs if abs(row[3]) > POWER_STEP]
    pct = 100 * len(outliers) / max(1, len(diffs))
    print(f"\n  outliers > 1 power_step ({POWER_STEP}W): "
          f"{len(outliers)} of {len(diffs)} ({pct:.1f}%)")
    for ts, s, l, d in outliers[:30]:
        print(f"    {ts.strftime('%H:%M:%S')}  shadow={s:4d}  live={l:4d}  diff={d:+5d}")
    if len(outliers) > 30:
        print(f"    ... and {len(outliers) - 30} more")


def print_mode_timeline(by_entity):
    section("Mode timeline (transitions only)")
    for label, e in (("live  ", "zendure.operation_mode"),
                     ("shadow", "sensor.zendure_operation_mode_shadow")):
        rows = by_entity.get(e, [])
        print(f"  {label} ({e}):")
        if not rows:
            print("    no data")
            continue
        prev = None
        for ts, s in rows:
            if s != prev:
                print(f"    {ts.strftime('%H:%M:%S')}  {s}")
                prev = s


def print_bypass(by_entity):
    section("Bypass active (4-state)")
    rows = by_entity.get("sensor.zendure_bypass_active", [])
    if rows:
        counts = Counter(s for _, s in rows)
        print(f"  state distribution: {dict(counts)}")
        prev = None
        for ts, s in rows:
            if s != prev:
                print(f"    {ts.strftime('%H:%M:%S')}  {s}")
                prev = s
    else:
        print("  no data")

    section("Bypass reached_at")
    rows = by_entity.get("sensor.zendure_bypass_reached_at", [])
    if rows:
        for ts, s in rows:
            print(f"  written at {ts.strftime('%H:%M:%S')} -> {s}")
    else:
        print("  no data")


def main(path):
    by_entity = load_csv(path)
    print_overview(by_entity)
    print_setpoint_diff(by_entity)
    print_mode_timeline(by_entity)
    print_bypass(by_entity)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} /path/to/history.csv", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
