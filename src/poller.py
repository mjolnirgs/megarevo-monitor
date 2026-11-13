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

import json
import shutil
import signal
import struct
import subprocess
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


def read_point(
    transport,
    point: dict[str, Any],
    retries: int = 1,
    retry_delay: float = 0.3,
) -> float | int | None:
    """Read one point, retrying up to `retries` times on the same
    already-open connection before giving up for this cycle.

    Added (2026-09-30) after journalctl analysis on real deployment logs
    showed read failures were heavily concentrated on whichever point is
    read FIRST in a cycle (soc_percent, first in points: order) and, to a
    lesser extent, second (battery_voltage) — roughly a 28%/11% failure
    rate for those two vs. ~1-2% for everything read after them in the
    same cycle. Process restarts were ruled out (11 in 2 days, not a
    crash loop) — this tracks position within a cycle, not which
    register. The poller opens a fresh TCP connection to the dongle every
    cycle (deliberately — see the note in run() about outliving a dead
    connection), and the dongle's Modbus-over-Solarman stack apparently
    isn't always ready to answer the first request or two right after
    that new connection, then answers reliably for the rest of the same
    cycle. A short retry on the same connection (not a reconnect) is
    exactly what the data supports fixing this with.
    """
    count = 2 if point["datatype"] in ("u32", "i32", "f32") else 1
    fc = point["fc"]
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if fc == 3:
                regs = transport.read_holding_registers(point["address"], count)
            elif fc == 4:
                regs = transport.read_input_registers(point["address"], count)
            else:
                raise ValueError(f"unsupported fc {fc} for point {point['name']}")
            return decode_point(regs, point["datatype"], point.get("scale", 1))
        except ModbusTransportError as e:
            last_err = e
            if attempt < retries:
                time.sleep(retry_delay)
    log.warning("Read failed for %s after %d attempt(s): %s", point["name"], retries + 1, last_err)
    return None


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


def write_nut_dummy_file(readings: dict[str, Any], battery_charge_low: float, on_grid_confirmed: bool) -> None:
    """Write a NUT dummy-ups "dummy mode" data file: plain upsc-style
    `variable: value` lines, one per line. The dummy-ups driver watches
    this file's mtime and reloads whenever it changes — this is what lets
    NUT treat the poller's telemetry as though it came from a real UPS
    talking to upsd, without writing a custom NUT driver.

    ups.status carries the two flags upsmon actually acts on: "OB" (on
    battery — grid_frequency_hz has no confirmed reference, see
    GridLossWatchdog) and "LB" (low battery — soc_percent at or below
    battery_charge_low). upsmon triggers FSD (forced shutdown) on clients
    when it sees "OB LB" together, which is the whole point of this file.

    on_grid_confirmed is the debounced signal from GridLossWatchdog, not a
    raw instantaneous grid_frequency_hz reading — the same brief false
    dropouts that caused spurious Nagios grid-status alerts would
    otherwise just as easily flip this to "OB" too (and, on a host set up
    to shed early on an on-battery timer, schedule a shutdown over a
    reading that was never a real outage).
    """
    soc = readings.get("soc_percent")
    batt_v = readings.get("battery_voltage")
    batt_w = readings.get("battery_power_w")

    on_grid = on_grid_confirmed
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
        now = time.time()
        if soc_percent is None:
            # A transient missing reading (e.g. Solarman dongle read
            # contention) must NOT reset in-progress decline tracking —
            # only a real uptick (a genuine recovery) should clear it.
            # Previously this wiped _peak_soc/_decline_since on every null,
            # which meant an intermittently-null poll happening more than
            # once inside the decline window could prevent the watchdog
            # from ever firing at all, no matter how long a real decline
            # actually ran for. Just report current state without letting
            # the gap itself advance or erase the clock.
            if self._decline_since is None:
                return False, 0.0
            elapsed = now - self._decline_since
            return elapsed >= self.threshold_s, elapsed

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


class GridLossWatchdog:
    """Confirms grid loss only after `confirm_seconds` of continuous
    no-reference readings, instead of flipping to CRITICAL/"on battery" on
    a single noisy poll cycle.

    Found necessary in practice: grid-status and the NUT dummy-ups status
    both keyed directly off an instantaneous `grid_frequency_hz > 40`
    check, which produced a real "on battery" false alarm roughly twice a
    day with a perfectly healthy grid connection — almost certainly a
    single bad/missed Modbus read (or one genuinely noisy register value)
    rather than an actual sub-second outage, since the battery has ample
    runway and there was never a corresponding real event. A missing
    reading (None, e.g. a failed Modbus read that cycle) counts the same
    as a low/zero reading here — both mean "no confirmed grid reference
    this cycle" — so a transient read failure alone can't produce a false
    confirmation either; it just extends how long the debounce window has
    to hold before firing.

    Any single good reading (grid_hz > 40) immediately resets the timer —
    this only ever delays flagging a loss, never delays recognizing
    recovery.
    """

    def __init__(self, confirm_seconds: float = 60):
        self.confirm_s = confirm_seconds
        self._off_since: float | None = None

    def update(self, grid_hz: float | None) -> tuple[bool, float]:
        """Returns (on_grid_confirmed, seconds_since_last_confirmed_on)."""
        now = time.time()
        if grid_hz is not None and grid_hz > 40:
            self._off_since = None
            return True, 0.0

        if self._off_since is None:
            self._off_since = now
        elapsed = now - self._off_since
        return elapsed < self.confirm_s, elapsed


class DongleRestartTracker:
    """Detects Solarman dongle restarts from its power-on counter (see
    SolarmanTransport._note_v5_response). The dongle is powered by the
    inverter, so a reset that lines up with a backup-output outage is
    evidence the inverter's own supply went down too, rather than just its
    output relay opening — useful when the inverter's event log records
    nothing at all.

    Deliberately clock-free: the monitoring host may itself have just
    rebooted (it can sit on the same backup output) with its clock not yet
    NTP-synced, so wall-clock arithmetic is unreliable exactly when this
    matters most. Within a process it compares against time.monotonic();
    across poller restarts it only trusts "the counter went backwards"
    (a value lower than the last one saved in state.json can only mean a
    reset)."""

    def __init__(self, last_saved_power_on_s: int | None, tolerance_s: float = 120):
        self.tol = tolerance_s
        self._saved = last_saved_power_on_s
        self._last_pon: int | None = None
        self._last_mono: float | None = None
        self.last_restart_detected: str | None = None
        # Uptime (seconds) the dongle had reached just before its most recent
        # detected reboot — i.e. how long it had been up when it reset. This
        # is what tells a routine timed self-reboot (uptime ~= the dongle's
        # fixed cadence) apart from one forced early by something external
        # (short uptime). run() uses it to classify; it does NOT log here, so
        # classification + log level live in one place with the correlation.
        self.last_reboot_uptime_s: int | None = None

    def update(self, power_on_s: int | None) -> bool:
        """Returns True on the cycle a restart is first detected. Sets
        last_reboot_uptime_s to the pre-reboot uptime on that cycle."""
        if power_on_s is None:
            return False
        now_mono = time.monotonic()
        restarted = False
        prev: int | None = None
        if self._last_pon is None:
            # First reading in this process: only the backwards check is safe.
            if self._saved is not None and power_on_s + self.tol < self._saved:
                restarted = True
                prev = self._saved
        else:
            expected = self._last_pon + (now_mono - self._last_mono)
            if power_on_s + self.tol < expected:
                restarted = True
                prev = self._last_pon
        if restarted:
            self.last_restart_detected = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.last_reboot_uptime_s = int(prev) if prev is not None else None
        self._last_pon, self._last_mono = power_on_s, now_mono
        return restarted


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _read_ups_status(ups: str) -> set[str]:
    """Best-effort read of a NUT UPS's ``ups.status`` flags via the ``upsc``
    CLI, e.g. {"OL"} on line power or {"OB"} / {"OB", "LB"} on battery.
    Returns an empty set if ``ups`` is unset, ``upsc`` isn't installed, or the
    read fails — correlation is a diagnostic nicety and must never raise or
    stall the poll loop. Used to tell whether the thing that senses the
    inverter's backup output (typically an APC on the monitoring host) was on
    battery around the same moment the dongle rebooted: a dongle reboot that
    lines up with a backup-output drop means the inverter itself restarted,
    whereas a backup-output drop with NO dongle reboot means the inverter kept
    running and only the Load1 feed opened."""
    if not ups:
        return set()
    exe = shutil.which("upsc")
    if not exe:
        return set()
    try:
        res = subprocess.run(
            [exe, ups, "ups.status"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 - diagnostics only, never break a cycle
        return set()
    if res.returncode != 0:
        return set()
    return set(res.stdout.split())


def _load_previous_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


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
    conn_cfg = cfg.get("connection", {})
    read_retries = conn_cfg.get("read_retries", 1)
    read_retry_delay_s = conn_cfg.get("read_retry_delay_s", 0.3)

    watchdog = DischargeWatchdog(
        thresholds.get("discharge_decline_minutes", 30),
        thresholds.get("discharge_decline_min_drop_pct", 1.0),
    )
    grid_watchdog = GridLossWatchdog(thresholds.get("grid_loss_confirm_seconds", 60))
    prev_dongle = _load_previous_state().get("dongle") or {}
    dongle_tracker = DongleRestartTracker(prev_dongle.get("power_on_s"))
    dongle_tracker.last_restart_detected = prev_dongle.get("last_restart_detected")

    # --- dongle health / reboot-correlation config (Solarman only) ---
    dongle_cfg = cfg.get("dongle", {})
    # Page only if the dongle stays UNREACHABLE (no valid response) this long.
    # A dongle *reboot* on its own never alerts — routine Solarman self-reboots
    # recover in under a minute, so they never trip this; a dongle or inverter
    # that genuinely doesn't come back does.
    dongle_down_alert_s = dongle_cfg.get("down_alert_minutes", 10) * 60
    # The dongle's own fixed self-reboot period, if it has one (0 = unknown).
    # A reboot whose pre-reboot uptime is within tolerance of this is "routine"
    # (the scheduled timer fired); one that fires well EARLY is off-cadence and
    # points at something external rebooting it.
    dongle_cadence_s = dongle_cfg.get("reboot_cadence_minutes", 0) * 60
    dongle_cadence_tol_s = dongle_cfg.get("reboot_cadence_tolerance_minutes", 20) * 60
    # A NUT UPS (e.g. "apc@localhost") that senses the inverter's backup
    # output. If set, each cycle checks whether it is on battery, so a dongle
    # reboot can be correlated with a Load1/backup-output drop. Empty disables
    # correlation entirely (the poll loop is unaffected either way).
    dongle_corr_ups = (dongle_cfg.get("correlate_ups") or "").strip()
    dongle_corr_window_s = dongle_cfg.get("correlate_window_seconds", 180)
    # How long the dongle-uptime check latches WARNING after a correlated
    # inverter-reboot so it's visible in Nagios, then self-clears.
    dongle_outage_latch_s = dongle_cfg.get("outage_latch_minutes", 15) * 60

    # Monotonic timestamp of the last cycle the dongle gave a valid response.
    # Assume reachable at startup so a brief first-cycle miss can't page.
    dongle_last_seen_mono = time.monotonic()
    # Monotonic timestamp the correlation UPS was last seen on battery (OB).
    dongle_last_ob_mono: float | None = None
    # Latch + description for the most recent correlated inverter reboot.
    dongle_outage_latch_until = 0.0
    dongle_outage_desc = ""
    # Classification of the most recent dongle reboot, carried in state.json:
    # "routine" | "anomalous" | "inverter_outage".
    dongle_last_reboot_kind = prev_dongle.get("last_reboot_kind")
    dongle_last_reboot_load1 = prev_dongle.get("last_reboot_load1_dropped")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    conn_type = conn_cfg.get("type", "?")
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
            readings[point["name"]] = read_point(transport, point, read_retries, read_retry_delay_s)

        dongle_pon = getattr(transport, "dongle_power_on_s", None)

        # Correlation input: is the backup-output sensor (APC) on battery?
        # Read it every cycle so a brief Load1 drop is caught even though the
        # dongle-reboot detection that pairs with it lands a cycle or two
        # later. Best-effort; empty/!configured -> no correlation.
        now_mono = time.monotonic()
        if dongle_corr_ups:
            if "OB" in _read_ups_status(dongle_corr_ups):
                dongle_last_ob_mono = now_mono

        restarted = dongle_tracker.update(dongle_pon)
        if restarted:
            uptime_at_reset = dongle_tracker.last_reboot_uptime_s
            in_cadence = (
                dongle_cadence_s > 0
                and uptime_at_reset is not None
                and abs(uptime_at_reset - dongle_cadence_s) <= dongle_cadence_tol_s
            )
            load1_dropped = (
                dongle_last_ob_mono is not None
                and (now_mono - dongle_last_ob_mono) <= dongle_corr_window_s
            )
            up_str = _fmt_duration(uptime_at_reset) if uptime_at_reset is not None else "?"
            if load1_dropped:
                # Dongle reset AND the backup output dropped together =
                # the inverter itself rebooted (its own supply died, taking
                # the dongle down with it). This is the event worth surfacing.
                dongle_reboot_kind = "inverter_outage"
                dongle_outage_desc = (
                    f"inverter reboot: Load1/backup output dropped and the dongle reset "
                    f"after only {up_str} uptime"
                    + (f" (off its ~{dongle_cadence_s // 60}min self-reboot cadence)" if dongle_cadence_s else "")
                )
                dongle_outage_latch_until = now_mono + dongle_outage_latch_s
                log.warning("Correlated %s", dongle_outage_desc)
            elif dongle_cadence_s > 0 and not in_cadence:
                # Rebooted off-schedule but the backup output stayed up, so the
                # inverter kept running — just the dongle glitching. Noted, not
                # alerted.
                dongle_reboot_kind = "anomalous"
                log.info(
                    "Off-cadence dongle reboot (uptime %s, cadence ~%dmin) but no Load1 drop "
                    "— dongle glitch, inverter unaffected.",
                    up_str, dongle_cadence_s // 60,
                )
            else:
                dongle_reboot_kind = "routine"
                log.info(
                    "Routine dongle self-reboot (uptime %s) — no alert.", up_str
                )
            dongle_last_reboot_kind = dongle_reboot_kind
            dongle_last_reboot_load1 = load1_dropped

        # Track how long the dongle has gone without a valid response. A
        # single missed cycle is normal; only a sustained gap is alertable.
        if dongle_pon is not None:
            dongle_last_seen_mono = now_mono
        dongle_down_s = now_mono - dongle_last_seen_mono

        if not hold_connection_open:
            transport.close()

        state = {
            "timestamp": cycle_start,
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(cycle_start)),
            "readings": readings,
        }
        if dongle_pon is not None or prev_dongle:
            state["dongle"] = {
                # Carry the last good value forward through a cycle with no
                # valid frame, so the next poller start still has it.
                "power_on_s": dongle_pon if dongle_pon is not None else prev_dongle.get("power_on_s"),
                "last_restart_detected": dongle_tracker.last_restart_detected,
                # Classification of that last reboot, for at-a-glance triage:
                #   routine         - the dongle's own timed self-reboot
                #   anomalous       - off-cadence, but backup output stayed up
                #   inverter_outage - off-cadence AND Load1/backup output dropped
                "last_reboot_uptime_s": dongle_tracker.last_reboot_uptime_s,
                "last_reboot_kind": dongle_last_reboot_kind,
                "last_reboot_load1_dropped": dongle_last_reboot_load1,
            }
            prev_dongle = state["dongle"]
        atomic_write_json(STATE_FILE, state)
        log.debug("Wrote state: %s", readings)

        grid_configured = "grid_frequency_hz" in readings
        grid_hz = readings.get("grid_frequency_hz")
        on_grid_confirmed, off_elapsed = grid_watchdog.update(grid_hz)

        if nut_enabled:
            write_nut_dummy_file(readings, nut_battery_charge_low, on_grid_confirmed)

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
            # breaker-off test): grid_voltage stayed present (~120V) with
            # the inverter's own breaker open, because this inverter senses
            # grid voltage via CT sensors at the service entry — upstream
            # of the inverter's breaker in the panel chain (inverter <->
            # inverter breaker/sub-panel <-> main building panel/breaker
            # <-> utility grid) — so it never reflects a real outage there.
            # grid_frequency_hz dropped to 0 in the same test (the
            # inverter's own loss of synchronization), which is the real
            # signal.
            #
            # Debounced through GridLossWatchdog (confirm window:
            # thresholds.grid_loss_confirm_seconds, default 60s) rather
            # than reacting to a single instantaneous reading — an
            # unconfirmed noisy/missed reading was producing a real "on
            # battery" false alarm about twice a day with nothing actually
            # wrong. `submit_passive_check` still fires every cycle so
            # Nagios sees a fresh OK during that debounce window, not a
            # freshness gap.
            if grid_configured:
                status = 0 if on_grid_confirmed else 2
                if on_grid_confirmed:
                    msg = "OK: grid present"
                else:
                    msg = f"CRITICAL: no confirmed grid reference for {off_elapsed:.0f}s (last grid_frequency_hz={grid_hz})"
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

            # Solarman only. This check does NOT alert on a dongle reboot by
            # itself — many Solarman loggers self-reboot on a fixed cycle
            # (recovering in under a minute) with the inverter unaffected, so
            # a short counter reset is routine noise. It pages only when the
            # dongle stays UNREACHABLE (no valid response) for longer than
            # thresholds.dongle_down_alert_minutes, which means the poller is
            # genuinely blind — the dongle or the inverter didn't come back.
            # A real inverter outage that drops the backup output is caught
            # separately and immediately by the UPS sensing that output; this
            # is just the slow backstop for "never recovered". Current uptime
            # is still reported as perfdata so reboots remain visible on a
            # graph without raising an alert.
            if "dongle_uptime" in services:
                disp_pon = dongle_pon if dongle_pon is not None else (
                    prev_dongle.get("power_on_s") if prev_dongle else None
                )
                up_perf = f"uptime={disp_pon}s" if disp_pon is not None else "uptime=U"
                perf = f"|{up_perf};;;0; down={int(dongle_down_s)}s;{dongle_down_alert_s};;0;"
                if dongle_down_s > dongle_down_alert_s:
                    # Backstop: the dongle genuinely isn't answering.
                    status, msg = 1, (
                        f"WARNING: Solarman dongle unreachable for {_fmt_duration(dongle_down_s)} "
                        f"(no valid response) - poller is blind; check the dongle and the inverter{perf}"
                    )
                elif now_mono < dongle_outage_latch_until:
                    # Correlated inverter reboot, latched briefly so Nagios
                    # shows it then self-clears. The backup-output UPS alerts
                    # instantly on its own; this corroborates the cause.
                    status, msg = 1, f"WARNING: {dongle_outage_desc}{perf}"
                elif disp_pon is not None:
                    status, msg = 0, f"OK: Solarman dongle up {_fmt_duration(disp_pon)}{perf}"
                else:
                    status, msg = 0, f"OK: Solarman dongle reachable{perf}"
                submit_passive_check(command_file, host_name, services["dongle_uptime"], status, msg)

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
