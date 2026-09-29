#!/usr/bin/env python3
"""
Nagios plugin: reads /var/lib/megarevo-monitor/state.json and reports a
single field's status. Does NOT touch the Modbus bus directly — that's the
poller's job, running as its own long-lived process so it can maintain the
discharge-watchdog's time window across polls. This script is only ever
invoked by Nagios as a fallback active check (e.g. `check_command
check_megarevo!soc`) if you want one in addition to the poller's passive
submissions, or for manual troubleshooting from the CLI.

Exit codes follow Nagios convention: 0=OK 1=WARNING 2=CRITICAL 3=UNKNOWN.
"""
import argparse
import json
import sys
import time
from pathlib import Path

# Reuse common.py's path logic rather than hardcoding it a second time —
# this plugin must resolve the exact same state.json the poller writes,
# including honoring MEGAREVO_VAR_DIR if it's set to something non-default.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
try:
    from common import STATE_FILE
except ImportError:
    # Fallback if run standalone without the sibling src/ dir present
    STATE_FILE = Path("/var/lib/megarevo-monitor/state.json")

MAX_AGE_S = 180  # if state.json is older than this, treat as UNKNOWN/stale


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("field", choices=["soc", "grid", "discharge_watchdog"])
    ap.add_argument("--warn", type=float, default=30.0)
    ap.add_argument("--crit", type=float, default=15.0)
    args = ap.parse_args()

    if not STATE_FILE.exists():
        print("UNKNOWN: state.json not found — has the poller run yet?")
        return 3

    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"UNKNOWN: could not read state.json: {e}")
        return 3

    age = time.time() - state.get("timestamp", 0)
    if age > MAX_AGE_S:
        print(f"UNKNOWN: state.json is {age:.0f}s old (poller may be down, or the link to the inverter/dongle is lost)")
        return 3

    readings = state.get("readings", {})

    if args.field == "soc":
        soc = readings.get("soc_percent")
        if soc is None:
            print("UNKNOWN: soc_percent not present in state — is it configured in points?")
            return 3
        if soc <= args.crit:
            print(f"CRITICAL: battery SOC {soc:.0f}%")
            return 2
        if soc <= args.warn:
            print(f"WARNING: battery SOC {soc:.0f}%")
            return 1
        print(f"OK: battery SOC {soc:.0f}%")
        return 0

    if args.field == "grid":
        grid_v = readings.get("grid_voltage")
        if grid_v is None:
            print("UNKNOWN: grid_voltage not present in state")
            return 3
        if grid_v <= 50:
            print(f"CRITICAL: grid voltage {grid_v:.1f}V — on battery")
            return 2
        print(f"OK: grid voltage {grid_v:.1f}V")
        return 0

    # discharge_watchdog state lives inside the poller process, not
    # state.json — this active-check path can't see it. Use the passive
    # check the poller submits directly instead.
    print("UNKNOWN: discharge_watchdog is only available as a passive check from the poller")
    return 3


if __name__ == "__main__":
    sys.exit(main())
