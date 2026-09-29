#!/usr/bin/env python3
"""
Raw Modbus register dump for the Megarevo R8KLNA, over either the Solarman
WiFi dongle (default — no wiring needed, just LAN reachability) or a wired
RS485 connection to the RS485_METER port.

Purpose: find out which (unit id, function code, address) triples carry
real data, WITHOUT trusting any single GoodWe register-map PDF, because
those PDFs disagree with each other across GoodWe's product history
(legacy DT ~768, LV-ET input ~3000, HV-ET ~35000/37000+). We don't know
which lineage Megarevo's firmware actually implements.

Run this with the Megarevo phone app open and visible, so you can
cross-check printed values against what the app shows *at the same
moment*. SOC, battery voltage, PV power, and grid power are the easiest
fields to eyeball-match confidently.

Writes a full raw dump (every register read, hex + decoded interpretations)
to /var/lib/megarevo-monitor/discover/<timestamp>.json for later reference,
and prints a live, human-scannable table to stdout.

This is READ-ONLY. It only ever issues function code 3 (read holding
registers) or 4 (read input registers) requests — never a write. Nothing
here can change an inverter setting.
"""
from __future__ import annotations

import argparse
import datetime
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import DISCOVER_DIR, ensure_dirs, setup_logging  # noqa: E402
from transport import ModbusTransportError, SerialTransport, SolarmanTransport  # noqa: E402

# Candidate register windows, named after which GoodWe product lineage
# documents them. Each tuple is (start_address, count_of_registers).
# Kept to modest windows and read in small chunks so we don't hammer a
# live production inverter for minutes on end.
PRESETS: dict[str, list[tuple[int, int]]] = {
    "legacy_dt": [(768, 124)],  # MiG-41 GoodWe-DT map, fc=3
    "goodwe_lv_input": [(3000, 130)],  # GoodWe EH/ET low-voltage input regs, fc=4
    "goodwe_hv_input": [(35100, 130), (37000, 140)],  # GoodWe ET/EH high-voltage, fc=4
    "goodwe_hv_holding": [(45000, 130), (47500, 60)],  # GoodWe HV holding regs, fc=3
}
PRESETS["all"] = [w for windows in PRESETS.values() for w in windows]

BAUD_CANDIDATES = [9600, 4800, 2400]  # serial transport only; GoodWe default is 9600
UNIT_CANDIDATES = [1, 247]  # GoodWe factory default is 247; 1 is the other common default

CHUNK_SIZE = 40  # registers per request; conservative for RTU/V5 framing
INTER_REQUEST_DELAY_S = 0.15  # be gentle with a live production inverter


def decode_variants(raw_regs: list[int]) -> dict[str, list]:
    """A handful of plausible interpretations of a register block so a human
    can eyeball-match against the app: raw u16, signed i16, and (for
    consecutive pairs) u32/f32 in both word orders."""
    out: dict[str, list] = {"u16": list(raw_regs), "i16": []}
    for r in raw_regs:
        out["i16"].append(r - 0x10000 if r >= 0x8000 else r)

    u32_be, u32_le, f32_be = [], [], []
    for i in range(0, len(raw_regs) - 1):
        hi, lo = raw_regs[i], raw_regs[i + 1]
        be_bytes = struct.pack(">HH", hi, lo)
        le_bytes = struct.pack(">HH", lo, hi)
        u32_be.append(struct.unpack(">I", be_bytes)[0])
        u32_le.append(struct.unpack(">I", le_bytes)[0])
        try:
            f32_be.append(round(struct.unpack(">f", be_bytes)[0], 3))
        except struct.error:
            f32_be.append(None)
    out["u32_word_be"] = u32_be
    out["u32_word_le"] = u32_le
    out["f32_word_be"] = f32_be
    return out


def scan(transport, fc: int, start: int, count: int, log) -> list[dict]:
    results = []
    addr = start
    remaining = count
    while remaining > 0:
        n = min(CHUNK_SIZE, remaining)
        try:
            if fc == 3:
                regs = transport.read_holding_registers(addr, n)
            else:
                regs = transport.read_input_registers(addr, n)
        except ModbusTransportError as e:
            log.debug("fc=%s addr=%s..%s -> no response (%s)", fc, addr, addr + n - 1, e)
            regs = None
        if regs:
            entry = {
                "fc": fc,
                "start_address": addr,
                "count": len(regs),
                "decoded": decode_variants(regs),
            }
            results.append(entry)
            log.info("fc=%s addr=%s..%s -> %s registers OK", fc, addr, addr + n - 1, len(regs))
        addr += n
        remaining -= n
        time.sleep(INTER_REQUEST_DELAY_S)
    return results


def print_table(results: list[dict], unit: int) -> None:
    if not results:
        print("\nNo registers responded for this unit.")
        return
    print(f"\n{'unit':>4} {'fc':>2} {'addr':>6}  u16 (first 12 values)")
    print("-" * 78)
    for r in results:
        u16 = r["decoded"]["u16"]
        preview = " ".join(f"{v:5d}" for v in u16[:12])
        more = " ..." if len(u16) > 12 else ""
        print(f"{unit:>4} {r['fc']:>2} {r['start_address']:>6}  {preview}{more}")


def run_windows(transport, unit: int, windows, fc_filter, log) -> list[dict]:
    all_results = []
    for fc in ([fc_filter] if fc_filter else [3, 4]):
        for start, count in windows:
            results = scan(transport, fc, start, count, log)
            if results:
                print(f"\n=== unit={unit} fc={fc} window={start}+{count} ===")
                print_table(results, unit)
                all_results.extend(results)
    return all_results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["solarman", "serial"], default="solarman")
    ap.add_argument("--preset", choices=list(PRESETS.keys()), default="all")
    ap.add_argument("--unit", type=int, action="append", help="override unit id candidates (repeatable)")
    ap.add_argument("--fc", type=int, choices=[3, 4], help="restrict to one function code")
    ap.add_argument("--start", type=int, help="single custom window: start address")
    ap.add_argument("--count", type=int, help="single custom window: register count")

    # solarman transport options
    ap.add_argument("--host", help="[solarman] dongle IP address on your LAN")
    ap.add_argument("--dongle-serial", type=int, help="[solarman] logger serial number printed on the dongle")
    ap.add_argument("--port", type=int, default=8899, help="[solarman] TCP port, default 8899")
    ap.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help=(
            "[solarman] seconds to wait per register window before giving up, default 3.0. "
            "Kept short here (vs. the poller's 10.0s default) because most windows in a full "
            "--preset all scan won't exist on your inverter at all, and each one that doesn't "
            "answer waits out the full timeout rather than failing fast."
        ),
    )

    # serial transport options
    ap.add_argument("--tty", help="[serial] e.g. /dev/megarevo-rs485 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, action="append", help="[serial] override baud candidates (repeatable)")

    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Log every request/response at DEBUG, including [solarman] the raw SENT/RECD hex "
            "frames from pysolarmanv5 and any V5 frame validation failure (bad checksum, "
            "sequence-number mismatch, wrong logger serial) that would otherwise fail silently "
            "as just 'no response'. Use this when a scan gets zero results despite the dongle "
            "being reachable (e.g. `nc -zv <host> 8899` succeeds) — that almost always means "
            "something's wrong at the V5 protocol layer, not the TCP layer, and this is the "
            "only way to see what."
        ),
    )

    args = ap.parse_args()

    if args.transport == "solarman" and (not args.host or args.dongle_serial is None):
        ap.error("--transport solarman requires --host and --dongle-serial")
    if args.transport == "serial" and not args.tty:
        ap.error("--transport serial requires --tty")

    ensure_dirs()
    log = setup_logging("discover")
    if args.verbose:
        log.setLevel("DEBUG")

    units = args.unit or UNIT_CANDIDATES
    if args.start is not None and args.count is not None:
        windows = [(args.start, args.count)]
    else:
        windows = PRESETS[args.preset]

    all_results = []
    found_any = False

    if args.transport == "solarman":
        for unit in units:
            transport = SolarmanTransport(
                host=args.host,
                dongle_serial=args.dongle_serial,
                unit=unit,
                port=args.port,
                timeout=args.timeout,
                logger=log,
                verbose=args.verbose,
            )
            try:
                transport.connect()
            except ModbusTransportError as e:
                log.warning("unit=%s: %s", unit, e)
                continue
            try:
                results = run_windows(transport, unit, windows, args.fc, log)
                if results:
                    found_any = True
                    all_results.extend(results)
            finally:
                transport.close()
    else:
        bauds = args.baud or BAUD_CANDIDATES
        for baud in bauds:
            log.info("Trying baud=%s", baud)
            for unit in units:
                transport = SerialTransport(
                    port=args.tty, baudrate=baud, parity="N", stopbits=1, bytesize=8, timeout=2.0, unit=unit
                )
                if not transport.connect():
                    log.warning("Could not open serial port %s at baud %s", args.tty, baud)
                    continue
                try:
                    results = run_windows(transport, unit, windows, args.fc, log)
                    if results:
                        found_any = True
                        all_results.extend(results)
                finally:
                    transport.close()
            if found_any:
                log.info("Got responses at baud=%s; not trying other baud rates.", baud)
                break

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dump_path = DISCOVER_DIR / f"{ts}.json"
    import json

    with open(dump_path, "w") as f:
        json.dump({"transport": args.transport, "results": all_results}, f, indent=2, default=str)
    log.info("Raw dump written to %s", dump_path)

    if not found_any:
        if args.transport == "solarman":
            print(
                "\nNothing responded. Check: dongle IP is correct and reachable "
                "(ping it), dongle serial number is the LOGGER's serial (printed "
                "on the dongle itself), not the inverter's, and that port 8899 "
                "isn't blocked between the Pi and the dongle."
            )
        else:
            print(
                "\nNothing responded. Check: RS485_A/B on pins 7/8, wiring "
                "polarity (swap A/B and retry), and the adapter device path."
            )
        return 1

    print(
        f"\nFull raw dump saved to {dump_path}\n"
        "Now cross-check the printed values against the Megarevo app at the same "
        "moment, and fill in /etc/megarevo-monitor/config.yaml with only the "
        "points you've confirmed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
