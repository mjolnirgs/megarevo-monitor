# megarevo-monitor

Modbus poller for the Megarevo R8KLNA, feeding passive checks into an
existing Nagios Core install. Built to run on a Nagios Pi (PoE, on
Load1), with all state/logs/venv confined to a USB SSD mounted at `/var`
so the SD card sees minimal writes.

Polls over the **existing Solarman WiFi dongle** on your LAN by default —
no wiring needed. The site's network gear is on the inverter Load1
backup power, and the dongle is already reachable over the LAN. A direct
RS485 wiring path is still supported as a documented fallback in case a
wired link ever makes sense (e.g. a future Pi placed near the inverter).

## Why "discover" comes before "poll"

Forum threads confirmed the inverter speaks GoodWe's "EMS protocol"
over Modbus — but GoodWe has shipped at least three incompatible register
layouts across its product history (legacy DT ~768, LV-ET input registers
~3000, HV-ET registers ~35000/37000+). Nobody has published which one
Megarevo's firmware actually implements. Given the whole earlier part of
this project was "the display said one thing, the real behavior was
different," this tool refuses to guess: `discover.py` dumps raw registers
you cross-check by eye against the phone app, and only confirmed points go
into `poller.py`'s config. This applies the same whether you're reading
over the WiFi dongle or RS485 — the transport changes, the register map
still has to be earned.

The register readouts are obtuse so I used claude heavilly here to decipher
them and locate the ones I needed by pasting the output of discover.py 

## Layout

```
/opt/megarevo-monitor/ venv + code (SD card is fine here, it's code not data)
/etc/megarevo-monitor/config.yaml connection info + verified register map + thresholds
/var/lib/megarevo-monitor/ state.json, discover dumps, sqlite history (SSD)
/var/log/megarevo-monitor/ rotated logs (SSD)
```

Only `/var/lib/megarevo-monitor` and `/var/log/megarevo-monitor` need to be
on the SSD-backed mount — that's enforced by the systemd unit
(`ReadWritePaths=` is scoped to exactly those two directories).

## Install

```bash
sudo useradd --system --home-dir /var/lib/megarevo-monitor --shell /usr/sbin/nologin megarevo
sudo mkdir -p /opt/megarevo-monitor /etc/megarevo-monitor \
              /var/lib/megarevo-monitor/discover /var/log/megarevo-monitor
sudo cp -r src/* /opt/megarevo-monitor/
sudo cp etc/config.example.yaml /etc/megarevo-monitor/config.yaml
sudo chown -R megarevo:megarevo /var/lib/megarevo-monitor /var/log/megarevo-monitor
sudo chown -R root:root /opt/megarevo-monitor /etc/megarevo-monitor

# venv lives on the SSD (/var) too, not the SD card
sudo -u megarevo python3 -m venv /var/lib/megarevo-monitor/venv
sudo -u megarevo /var/lib/megarevo-monitor/venv/bin/pip install -r requirements.txt
```

The `dialout` group membership and udev rule below are **only needed for
the RS485 fallback** — skip them if you're only ever going to poll the
WiFi dongle.

```bash
sudo usermod -aG dialout megarevo   # RS485 fallback only
```

## Step 1 — find the dongle's LAN IP and logger serial number

You need two things before you can poll:

- **The dongle's IP address on your LAN.** Check your router's DHCP lease
  list for the Solarman/logger device (it usually shows up with a
  Solarman-branded hostname), or the dongle broadcasts its own AP-mode SSID
  when it can't reach a router — connect to that once to see its assigned
  address, or just note the SSID and reconfigure it onto your LAN if it's
  not already joined. A static DHCP reservation for it is worth setting up
  so this doesn't drift.
- **The logger's serial number**, printed on a label on the dongle itself
  (it plugs into the inverter's datalogger slot). This is **not** the
  inverter's own serial number — they're different numbers, and using the
  inverter's SN here will fail silently (the dongle just won't answer).

Confirm TCP port 8899 is reachable from the Pi to the dongle's IP (same
LAN/VLAN, nothing blocking it):

```bash
nc -zv <dongle-ip> 8899
```

## Step 2 — discover

With the Megarevo app open on your phone showing live data:

```bash
sudo -u megarevo /var/lib/megarevo-monitor/venv/bin/python \
  /opt/megarevo-monitor/discover.py \
  --transport solarman --host <dongle-ip> --dongle-serial <logger-serial> \
  --preset all
```

This tries unit IDs 1 and 247 (GoodWe's factory default) across the three
candidate GoodWe register windows, and writes a timestamped raw dump to
`/var/lib/megarevo-monitor/discover/`. It also prints a live table to your
terminal.

Cross-check the printed values against what the app shows *at that same
moment* (SOC, battery voltage, PV power, grid power, load power are the
easiest to eyeball-match). Note down which `(unit, fc, address)` triple
corresponds to which real quantity.

Do **not** skip this step and hand-copy a register map from a GoodWe PDF

### RS485 fallback

If you ever do wire RS485 directly into the RS485_METER port (pins 7/8 —
RS485_A/RS485_B), install the udev rule first so the adapter gets a stable
name regardless of USB enumeration order:

```bash
sudo cp udev/99-megarevo-rs485.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Run `udevadm info -a -n /dev/ttyUSB0` (or whatever it enumerates as) to get
the real idVendor/idProduct/serial and edit the rule if it doesn't match
the FTDI-chip assumption baked into the example rule. Then:

```bash
sudo -u megarevo /var/lib/megarevo-monitor/venv/bin/python \
  /opt/megarevo-monitor/discover.py --transport serial --tty /dev/megarevo-rs485 --preset all
```

## Step 3 — fill in the verified map

Edit `/etc/megarevo-monitor/config.yaml`. Set the `connection:` block to
match how you're polling (solarman: `host`/`dongle_serial`/`port`; serial:
`port`/`baudrate`/etc — the example file documents both, with solarman as
the default). Each entry under `points:` needs `name`, `fc` (3=holding,
4=input), `address`, `datatype` (`u16`/`i16`/`u32`/`i32`), and `scale`.
Leave anything unverified commented out — the poller only reads what's
uncommented, and refuses to start with zero verified points.

## Step 4 — run the poller

```bash
sudo cp systemd/megarevo-poller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now megarevo-poller.service
sudo journalctl -u megarevo-poller -f
```

It polls on `poll_interval_seconds` (default 30s), writes
`/var/lib/megarevo-monitor/state.json` atomically, and — if
`nagios.enabled: true` in the config — submits passive check results
directly to your Nagios command pipe.

The poller reconnects fresh every cycle rather than holding one
persistent socket open (so it self-heals from a dead connection instead
of getting stuck on one until restarted), and on real deployments some
dongles need a moment to settle right after that reconnect — a failed
read is retried once automatically on the same connection
(`connection.read_retries`/`read_retry_delay_s` in config.yaml) before
being logged as a genuine miss for that cycle.

## Step 5 — wire into Nagios

See `nagios/megarevo.cfg` for a host + service template. It uses
**passive-only** checks with `check_freshness 1` — if the poller dies or
the link to the dongle (or RS485 adapter) drops, Nagios itself flags
staleness instead of silently holding a last-known-good value forever 

Confirm your Nagios command-file path (commonly
`/usr/local/nagios/var/rw/nagios.cmd`) and set it in
`nagios.command_file` in config.yaml — it varies by how Nagios was built.

## Checks included

- `battery-soc` — WARNING/CRITICAL on low SOC. `soc_percent` is confirmed at address 12613 (scale 0.1) on the author's own R8KLNA, found during a real charge/discharge cycle and cross-checked against the app three separate times within ~1%. An earlier guess at address 5793 stayed pinned at 100% during a real discharge (the app's SoC dropped to 99%) and is retired as likely SoH, not SoC. **Verify against your own unit before trusting this address blindly** — see the note at the top of `config.example.yaml`.
- `grid-status` — CRITICAL if grid is lost (this is your "on battery" signal for NUT/shutdown automation downstream). Keyed on `grid_frequency_hz`, not `grid_voltage` — a live breaker-off test found `grid_voltage` stays present with the inverter breaker open (it reads the utility feed from the CT sensors), while `grid_frequency_hz` correctly drops to 0. Debounced (`thresholds.grid_loss_confirm_seconds`, default 60s) rather than reacting to a single instantaneous reading — a lone noisy/missed read can otherwise produce a false "on battery" alarm with nothing actually wrong; this only ever delays flagging a loss, never delays recognizing recovery. The same debounced signal also drives the NUT dummy-ups "OB" flag, not a raw reading.
- `battery-soc-decline` — WARNING if SOC has sat below its most recent peak, with no recovery, for N minutes straight. Originally designed as a battery-power-vs-PV comparison; battery current/power seemed unreachable at first, so it fell back to watching SOC trend, which is actually closer to how the CAN charge-limit-latch bug was diagnosed in the first place ("SOC kept dropping, wouldn't recover without a power cycle"). Battery current/power were later found too (`battery_current_a`/`battery_power_w`, address 12607/12618) — a power-based redesign is worth considering now that both are real, confirmed telemetry, but SOC-trend still works and hasn't been changed. A transient missing SOC reading no longer resets the decline clock (it used to wipe out all progress on every null read, which could prevent this from ever firing if nulls happened more than once inside the window) — only a real uptick clears it.
- `battery-power` — informational only, no thresholds yet (no established baseline for what's abnormal). Reports live current/power/voltage with perfdata for graphing.
- `comms-freshness` — handled by Nagios' own `check_freshness`, not a poller-side check

## Step 6 — graceful shutdown of other hosts on low battery (optional)

Set `nut.enabled: true` in `config.yaml` and the poller will also write a
NUT (Network UPS Tools) `dummy-ups` data file each cycle, letting NUT
coordinate an automatic, graceful shutdown of other hosts on your network
once the battery hits a threshold you choose — with example client
templates for a plain shutdown, a host that sheds early on a fixed timer
regardless of SOC, and a Proxmox VE host that shuts its guest VMs down
cleanly via the QEMU guest agent first. See [`nut/README.md`](nut/README.md)
for the full setup and testing walkthrough, and email/Telegram alerting on
outage state changes.

## Hardware background / credit

The register discovery approach (and the observation that Megarevo
inverters speak a GoodWe-derived Modbus dialect without matching any of
GoodWe's published register maps) builds on community findings from the
DIY Solar Forum. If you're working with a different Megarevo model or
firmware revision, expect to re-run `discover.py` rather than assuming
this repo's register map transfers directly — see the note at the top of
`config.example.yaml`.

## License

MIT — see [LICENSE](LICENSE).

Contributions (additional confirmed register maps for other Megarevo
models, fixes, other integrations) are welcome via PR. If you confirm or
correct a register address on your own unit, please note how you verified
it (cross-checked against the app, a breaker-off test, etc.) — the whole
point of this project is not trusting an address that hasn't actually
been confirmed against real hardware behavior.
