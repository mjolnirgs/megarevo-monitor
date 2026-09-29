# NUT integration

Coordinates a graceful shutdown of other hosts on your network when the
battery gets low — using megarevo-monitor's own poller as the data source
instead of a physical UPS, via NUT's `dummy-ups` driver.

**How it fits together:** `poller.py` writes `/var/lib/megarevo-monitor/nut-dummy.dev`
every poll cycle (needs `nut.enabled: true` in config.yaml). NUT's
`dummy-ups` driver on your NUT server host (typically the same host
running the poller) watches that file and reloads it whenever it changes,
so `upsd` sees it as a live "UPS." Your target hosts run `upsmon` in
SECONDARY mode, polling that server's `upsd` over the network — but they
don't all have to shed at the same trigger. Three example client
templates are included:

| Client type | Config file | Trigger | Shutdown action |
|---|---|---|---|
| Plain | `upsmon-plain-client.conf` | SOC ≤ `battery_charge_low` (`OB LB`) | plain `shutdown -h +0` |
| Timed | `upsmon-timed-shutdown-client.conf` + `notify-timed-shutdown.sh` | N minutes on battery, regardless of SOC (SOC trigger stays wired as a safety net) | plain `shutdown -h +0`, scheduled via `shutdown -c` / `shutdown -h +N` on NUT's ONBATT/ONLINE events |
| Proxmox VE | `upsmon-proxmox-client.conf` + `shutdown-proxmox-vms.sh` | SOC ≤ `battery_charge_low` (`OB LB`) | explicit per-VM `qm shutdown` (uses qemu-guest-agent) before host poweroff |

The **timed** template is for a host that isn't needed during an outage —
shedding it early frees up battery runway for hosts that matter more.
`SHUTDOWN_DELAY_MINUTES` at the top of `notify-timed-shutdown.sh` controls
how long it waits before shutting down; the SOC-based safety net stays
active in case the battery drains faster than that budget assumes.

The **Proxmox** template assumes your guest VMs have `qemu-guest-agent`
installed and `agent: 1` set in their VM config — `shutdown-proxmox-vms.sh`
explains how to check, and how it degrades (falls back to ACPI shutdown)
if a VM doesn't have it.

Mix and match freely — most hosts probably want the plain template, with
the other two reserved for hosts that specifically need that behavior.

The server itself runs a PRIMARY monitor too (NUT's design requires
exactly one), but its `SHUTDOWNCMD` is a harmless log-only no-op — this
host needs to stay up and monitoring through the whole outage, not shut
itself down. That same PRIMARY monitor is also where email/Telegram
alerting is wired (`notify-alert.sh`, via `NOTIFYCMD`) — it fires once per
real event (on battery, back online, low battery, FSD, comms
lost/restored) rather than once per host, since only the primary sees the
canonical state. `notify_by_telegram.sh` is included as a minimal
reference Telegram sender if you don't already have one; email just needs
a working local MTA.

## Install — NUT server (same host as the poller, typically)

```bash
sudo apt install nut nut-server
sudo cp nut.conf /etc/nut/nut.conf
sudo cp ups.conf /etc/nut/ups.conf
sudo cp upsd.conf /etc/nut/upsd.conf
sudo cp upsd.users /etc/nut/upsd.users
sudo cp upsmon-primary.conf /etc/nut/upsmon.conf
sudo cp notify-alert.sh /usr/local/bin/notify-alert.sh
sudo cp notify_by_telegram.sh /usr/local/bin/notify_by_telegram.sh
sudo chmod 755 /usr/local/bin/notify-alert.sh /usr/local/bin/notify_by_telegram.sh

# EDIT before starting anything:
#   - upsd.users: set real passwords for [primary] and [secondary]
#   - upsmon.conf (the copy of upsmon-primary.conf): match the [primary] password
#   - upsd.conf: replace 192.0.2.10 with this host's real LAN IP
#   - notify-alert.sh: set EMAIL_RECIPIENT and TELEGRAM_CHAT_ID
#   - notify_by_telegram.sh: set TOKEN to your bot's token (or point
#     notify-alert.sh's TELEGRAM_SCRIPT at your own notifier instead)

sudo chown root:nut /etc/nut/upsd.users
sudo chmod 640 /etc/nut/upsd.users

sudo systemctl enable --now nut-server
sudo systemctl enable --now nut-monitor   # runs the local (primary) upsmon
sudo upsc megarevo@localhost              # should print the live readings
```

If `upsc` doesn't show data, check that `nut.enabled: true` is set in
`/etc/megarevo-monitor/config.yaml` and that
`/var/lib/megarevo-monitor/nut-dummy.dev` actually exists and is updating
(`watch -n5 cat /var/lib/megarevo-monitor/nut-dummy.dev`) before
troubleshooting NUT itself — most likely failure mode is the file not
being written yet, not a NUT config problem.

**Test the alerting independent of a real outage** once it's configured:
`NOTIFYTYPE=ONBATT /usr/local/bin/notify-alert.sh "manual test"` should
produce both an email and a Telegram message without needing to touch the
inverter's breaker at all.

**RHEL/Rocky/Fedora note:** on EL-family distros (via EPEL), NUT's config
directory is `/etc/ups/`, not `/etc/nut/` — same files, different path.
Check `systemctl list-unit-files | grep -i nut` too, since the service
unit name can differ from Debian's `nut-monitor.service`.

## Install — each target host

Same `nut-client` package and `nut.conf` (`MODE=netclient`) on every host,
but which `upsmon.conf` (and helper script, if any) you copy depends on
which behavior that host needs — see the table above.

**Plain:**

```bash
sudo apt install nut-client
sudo cp upsmon-plain-client.conf /etc/nut/upsmon.conf
# EDIT: match the [secondary] password from the server's upsd.users, and
# replace 192.0.2.10 with the server's real LAN IP
echo "MODE=netclient" | sudo tee /etc/nut/nut.conf
sudo systemctl enable --now nut-monitor
sudo upsc megarevo@<server-ip>
```

**Timed:**

```bash
sudo apt install nut-client
sudo cp upsmon-timed-shutdown-client.conf /etc/nut/upsmon.conf
sudo cp notify-timed-shutdown.sh /usr/local/bin/notify-timed-shutdown.sh
sudo chmod 755 /usr/local/bin/notify-timed-shutdown.sh
# EDIT upsmon.conf: match the [secondary] password and server IP as above
# EDIT notify-timed-shutdown.sh: set SHUTDOWN_DELAY_MINUTES
echo "MODE=netclient" | sudo tee /etc/nut/nut.conf
sudo systemctl enable --now nut-monitor
sudo upsc megarevo@<server-ip>
```

Test the timer independent of a real outage by watching `journalctl -u
nut-monitor -f` and briefly opening the inverter's grid breaker (or the
dry run below) — you should see `ONBATT` logged and a shutdown scheduled
(`shutdown --no-wall` isn't used, so logged-in users will see the
warning); reconnect and confirm it's cancelled (`ONLINE` logged, no
pending shutdown). Don't wait out the full delay to prove this — canceling
partway through and confirming the schedule/cancel logged correctly is
enough.

**Proxmox VE:**

```bash
sudo apt install nut-client
sudo cp upsmon-proxmox-client.conf /etc/nut/upsmon.conf
sudo cp shutdown-proxmox-vms.sh /usr/local/bin/shutdown-proxmox-vms.sh
sudo chmod 755 /usr/local/bin/shutdown-proxmox-vms.sh
# EDIT upsmon.conf: match the [secondary] password and server IP as above
echo "MODE=netclient" | sudo tee /etc/nut/nut.conf
sudo systemctl enable --now nut-monitor
sudo upsc megarevo@<server-ip>
```

Before trusting this, confirm every guest VM actually has `agent: 1` set
in its Proxmox config (not just the guest agent installed inside the VM):

```bash
for vmid in $(qm list | awk 'NR>1{print $1}'); do
    echo -n "$vmid: "; qm config "$vmid" | grep -q '^agent:.*1' && echo agent-enabled || echo "AGENT NOT ENABLED"
done
```

Any VM without `agent: 1` will fall back to ACPI shutdown, which is
usually fine but worth knowing about ahead of time rather than during a
real event.

## Testing before trusting it

Don't wait for a real outage to find out whether this works. Once your
hosts are configured:

1. Lower `nut.battery_charge_low` in `config.yaml` temporarily to just
   above the current live SOC (e.g. if SOC is 85%, set it to 84%) and
   restart `megarevo-poller` — this should flip `ups.status` to `OB LB`
   within one poll cycle without needing a real discharge test, letting
   you confirm the SOC-triggered chain (`upsc` on each client shows
   `OB LB` → `upsmon` logs an FSD event → `SHUTDOWNCMD` fires) safely, on
   your own schedule. This exercises the plain and Proxmox VE clients; it
   does NOT exercise the timed client, which only reacts to
   `ONBATT`/`ONLINE`, not `battery_charge_low`.
2. Set it back to the real value afterward.
3. Separately, test a timed client by briefly opening the inverter breaker
   (a few minutes is enough) and confirming `ONBATT` gets logged and a
   shutdown gets scheduled, then closing the breaker again and confirming
   it's cancelled.
4. Separately, dry-run a Proxmox client's VM shutdown path directly rather
   than through NUT at all: run `shutdown-proxmox-vms.sh`'s VM loop by
   hand (or just `qm shutdown <vmid>` on one test VM) and confirm it
   actually goes through the guest agent rather than falling back to ACPI
   silently.
5. Only after those succeed, consider a real breaker-off test, timing how
   long your plain/Proxmox clients actually take to finish shutting down
   at the SOC trigger — that's the number to check against the runway
   math in `config.example.yaml`'s `nut:` section comment. (A timed
   client's real-world timing is simpler — it's just whatever a normal
   shutdown of that host takes, since it's timer- not drain-driven.)
