#!/usr/bin/env python3
"""
Poll a verified Modbus RTU register map from the Megarevo R8KLNA and:
  1. write /var/lib/megarevo-monitor/state.json (atomic, for the Nagios
     check script and anything else to read)
  2. optionally submit passive check results directly into Nagios' command
     pipe
  3. optionally write /var/lib/megarevo-monitor/nut-dummy.dev, a NUT
     dummy-ups "dummy mode" data file that lets NUT treat this telemetry
     as a real UPS for coordinating graceful shutdowns across hosts

Refuses to start if config.yaml has zero verified `points` — that's the
guard against ever polling an address nobody has actually confirmed
against the real display.
"""
from __future__ import annotations

import signal
import struct
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from common import (  # noqa: E402
    NUT_DATA_FILE,
    STATE_FILE,
    atomic_write_json,
    atomic_write_text,
    ensure_dirs,
    load_config,
    setup_logging,
)
from transport import ModbusTransportError, make_transport  # noqa: E402

log = setup_logging("poller")

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("Received signal %s, shutting down after current cycle.", signum)
    _shutdown = True


def decode_point(regs: list[int], datatype: str, scale: float) -> float | int | None:
    if not regs:
        return None
    if datatype == "u16":
        val = regs[0]
    elif datatype == "i16":
        val = regs[0] - 0x10000 if regs[0] >= 0x8000 else regs[0]
    elif datatype in ("u32", "i32", "f32"):
        if len(regs) < 2:
            return None
        packed = struct.pack(">HH", regs[0], regs[1])
        if datatype == "u32":
            val = struct.unpack(">I", packed)[0]
        elif datatype == "i32":
            val = struct.unpack(">i", packed)[0]
        else:
            val = struct.unpack(">f", packed)[0]
    else:
        raise ValueError(f"unknown datatype {datatype}")
    return val * scale


def read_point(transport, point: dict[str, Any]) -> float | int | None:
    count = 2 if point["datatype"] in ("u32", "i32", "f32") else 1
    fc = point["fc"]
    try:
        if fc == 3:
            regs = transport.read_holding_registers(point["address"], count)
        elif fc == 4:
            regs = transport.read_input_registers(point["address"], count)
        else:
            raise ValueError(f"unsupported fc {fc} for point {point['name']}")
    except ModbusTransportError as e:
        log.warning("Read failed for %s: %s", point["name"], e)
        return None
    return decode_point(regs, point["datatype"], point.get("scale", 1))


def submit_passive_check(command_file: str, host_name: str, service: str, status: int, message: str) -> None:
    """Append a PROCESS_SERVICE_CHECK_RESULT external command to the Nagios
    command pipe. status: 0=OK 1=WARNING 2=CRITICAL 3=UNKNOWN."""
    ts = int(time.time())
    line = f"[{ts}] PROCESS_SERVICE_CHECK_RESULT;{host_name};{service};{status};{message}\n"
    try:
        with open(command_file, "a") as f:
            f.write(line)
    except OSError as e:
        log.error("Could not write to Nagios command file %s: %s", command_file, e)


def write_nut_dummy_file(readings: dict[str, Any], battery_charge_low: float) -> None:
    """Write a NUT dummy-ups "dummy mode" data file: plain upsc-style
    `variable: value` lines, one per line. The dummy-ups driver watches
    this file's mtime and reloads whenever it changes — this is what lets
    NUT treat the poller's telemetry as though it came from a real UPS
    talking to upsd, without writing a custom NUT driver.

    ups.status carries the two flags upsmon actually acts on: "OB" (on
    battery — grid_frequency_hz has no reference) and "LB" (low battery —
    soc_percent at or below battery_charge_low). upsmon triggers FSD
    (forced shutdown) on clients when it sees "OB LB" together, which is
    the whole point of this file.
    """
    soc = readings.get("soc_percent")
    batt_v = readings.get("battery_voltage")
    batt_w = readings.get("battery_power_w")
    grid_hz = readings.get("grid_frequency_hz")

    on_grid = grid_hz is not None and grid_hz > 40
    low_batt = soc is not None and soc <= battery_charge_low

    if on_grid:
        status = "OL"
    elif low_batt:
        status = "OB LB"
    else:
        status = "OB"

    lines = [
        "device.type: ups",
        "ups.mfr: Megarevo",
        "ups.model: R8KLNA",
        f"ups.status: {status}",
        f"battery.charge.low: {battery_charge_low:.0f}",
    ]
    if soc is not None:
        lines.append(f"battery.charge: {soc:.0f}")
    if batt_v is not None:
        lines.append(f"battery.voltage: {batt_v:.1f}")
    if batt_w is not None:
        lines.append(f"battery.power: {batt_w}")

    try:
        atomic_write_text(NUT_DATA_FILE, "\n".join(lines) + "\n")
    except OSError as e:
        log.error("Could not write NUT data file %s: %s", NUT_DATA_FILE, e)


class DischargeWatchdog:
    """Tracks whether SOC has been continuously declining, without ever
    ticking back up, for a sustained window — the exact symptom used to
    diagnose the CAN charge-limit-latch bug in the first place ("I've let
    it drop as far as 80% before power cycling"), rather than an
    instantaneous battery-power reading.

    Battery current/power turned out not to be reachable over this local
    Modbus/Solarman interface at all: a sweep of several thousand register
    addresses against the real inverter found voltage, SOC, grid, and PV
    fields matching the app cleanly, but nothing matching the app's live
    current/power readings at any address or scale. They likely only
    travel over the CAN bus to the BMS — the same bus the original bug
    lived on — not this RS485/meter-style interface. SOC trend ends up
    being a more faithful reproduction of how the bug was actually
    diagnosed than a power comparison would have been anyway.

    Fires WARNING if SOC has sat at least `min_drop_pct` below its most
    recent peak for `minutes` straight, regardless of root cause, as a
    regression guard — any uptick (charging catching back up) resets it."""

    def __init__(self, minutes: float, min_drop_pct: float = 1.0):
        self.threshold_s = minutes * 60
        self.min_drop_pct = min_drop_pct
        self._peak_soc: float | None = None
        self._decline_since: float | None = None

    def update(self, soc_percent: float | None) -> tuple[bool, float]:
        if soc_percent is None:
            self._peak_soc = None
            self._decline_since = None
            return False, 0.0

        now = time.time()
        if self._peak_soc is None or soc_percent >= self._peak_soc:
            # New high-water mark (or first reading ever) — a healthy
            # battery's SOC only goes up when charging, so any uptick
            # clears whatever decline was in progress.
            self._peak_soc = soc_percent
            self._decline_since = None
            return False, 0.0

        if soc_percent <= self._peak_soc - self.min_drop_pct:
            if self._decline_since is None:
                self._decline_since = now
            elapsed = now - self._decline_since
            return elapsed >= self.threshold_s, elapsed

        return False, 0.0


def run() -> int:
    ensure_dirs()
    cfg = load_config()
    points = cfg.get("points") or []
    if not points:
        log.error(
            "No verified points in config.yaml. Run discover.py, cross-check "
            "against the app, then uncomment confirmed points before running "
            "the poller. Refusing to poll unverified addresses."
        )
        return 1

    interval = cfg.get("poll_interval_seconds", 30)
    thresholds = cfg.get("thresholds", {})
    nagios_cfg = cfg.get("nagios", {})
    nagios_enabled = nagios_cfg.get("enabled", False)
    nut_cfg = cfg.get("nut", {})
    nut_enabled = nut_cfg.get("enabled", False)
    nut_battery_charge_low = nut_cfg.get("battery_charge_low", 25)

    watchdog = DischargeWatchdog(
        thresholds.get("discharge_decline_minutes", 30),
        thresholds.get("discharge_decline_min_drop_pct", 1.0),
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    conn_type = cfg.get("connection", {}).get("type", "?")
    log.info("Starting poller: connection=%s interval=%ss points=%s", conn_type, interval, [p["name"] for p in points])

    # During discovery, one run against the real dongle went completely
    # silent for a couple of minutes (TCP connected fine, requests just
    # never got a reply) and then started working again with no config
    # change — cause unconfirmed (ruled out: serial mismatch, wrong unit
    # ID, and the SolarMAN app holding the local connection slot, since the
    # dongle sits on an isolated VLAN the app can't even reach locally).
    # Reconnecting each cycle instead of holding one persistent socket open
    # is cheap for both transports and means a poller that outlives a
    # transient dongle-side hiccup like that one just tries again next
    # cycle rather than being stuck on a dead connection until restarted.
    hold_connection_open = False

    transport = make_transport(cfg)

    while not _shutdown:
        cycle_start = time.time()
        try:
            transport.connect()
        except ModbusTransportError as e:
            log.error("Could not connect (%s); retrying next cycle.", e)
            time.sleep(interval)
            continue

        readings: dict[str, Any] = {}
        for point in points:
            readings[point["name"]] = read_point(transport, point)

        if not hold_connection_open:
            transport.close()

        state = {
            "timestamp": cycle_start,
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(cycle_start)),
            "readings": readings,
        }
        atomic_write_json(STATE_FILE, state)
        log.debug("Wrote state: %s", readings)

        if nut_enabled:
            write_nut_dummy_file(readings, nut_battery_charge_low)

        if nagios_enabled:
            host_name = nagios_cfg["host_name"]
            services = nagios_cfg.get("services", {})
            command_file = nagios_cfg["command_file"]

            soc = readings.get("soc_percent")
            if soc is not None:
                if soc <= thresholds.get("soc_critical_pct", 15):
                    status, msg = 2, f"CRITICAL: battery SOC {soc:.0f}%"
                elif soc <= thresholds.get("soc_warning_pct", 30):
                    status, msg = 1, f"WARNING: battery SOC {soc:.0f}%"
                else:
                    status, msg = 0, f"OK: battery SOC {soc:.0f}%"
                if "soc" in services:
                    submit_passive_check(command_file, host_name, services["soc"], status, msg)

            # Keyed on frequency, not voltage. Confirmed live (2026-09-28,
            # breaker-off test): grid_voltage stayed present with the main
            # breaker off and the inverter running the house off battery —
            # it's reading downstream of the inverter's own EPS/output
            # side, not the actual utility feed, so it never reflects a
            # real outage. grid_frequency_hz dropped to 0 in the same test
            # (no utility waveform to lock to), which is the real signal.
            grid_hz = readings.get("grid_frequency_hz")
            if grid_hz is not None:
                on_grid = grid_hz > 40  # utility is ~60Hz; 0 means no reference at all
                status = 0 if on_grid else 2
                msg = "OK: grid present" if on_grid else f"CRITICAL: grid frequency {grid_hz:.2f}Hz — on battery"
                if "grid_status" in services:
                    submit_passive_check(command_file, host_name, services["grid_status"], status, msg)

            # Informational only — no thresholds yet, since these registers
            # (battery_current_a/battery_power_w) were only just found
            # (2026-09-28, live during a breaker-off discharge test) and we
            # don't have a baseline for what's normal vs. concerning. Always
            # reports OK; the value is in freshness tracking (catches the
            # poller dying) and the perfdata for graphing, not in alerting.
            # Sign convention observed so far: negative = discharging.
            # Unverified while charging — confirm positive = charging once
            # the battery's actually charging again before trusting it.
            batt_v = readings.get("battery_voltage")
            batt_a = readings.get("battery_current_a")
            batt_w = readings.get("battery_power_w")
            if "battery_power" in services and None not in (batt_v, batt_a, batt_w):
                direction = "charging" if batt_w > 0 else "discharging" if batt_w < 0 else "idle"
                msg = (
                    f"OK: {direction} {abs(batt_w)}W ({batt_a:+.1f}A @ {batt_v:.1f}V)"
                    f"|power={batt_w}W;;;; current={batt_a}A;;;; voltage={batt_v}V;;;;"
                )
                submit_passive_check(command_file, host_name, services["battery_power"], 0, msg)

            soc_for_watchdog = readings.get("soc_percent")
            fired, elapsed = watchdog.update(soc_for_watchdog)
            # Only submits when soc_percent is actually present — mirrors
            # the `soc` block above. Without this guard it would submit a
            # fake, permanent "OK" every cycle even with no real data
            # behind it (fired is always False when soc_for_watchdog is
            # None, so it would just silently claim everything's fine).
            if "discharge_watchdog" in services and soc_for_watchdog is not None:
                if fired:
                    status, msg = (
                        1,
                        f"WARNING: SOC declining for {elapsed/60:.0f}min without recovery "
                        f"(no uptick since last peak — same pattern as the charge-limit-latch bug)",
                    )
                else:
                    status, msg = 0, "OK"
                submit_passive_check(command_file, host_name, services["discharge_watchdog"], status, msg)

        elapsed_cycle = time.time() - cycle_start
        time.sleep(max(0.0, interval - elapsed_cycle))

    transport.close()
    log.info("Poller stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
