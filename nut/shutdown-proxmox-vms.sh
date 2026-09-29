#!/bin/sh
# /usr/local/bin/shutdown-proxmox-vms.sh — SHUTDOWNCMD target for a
# Proxmox VE host.
#
# Proxmox's own pve-guests service normally stops guest VMs as part of a
# plain `shutdown`, but this makes the guest-agent shutdown explicit and
# visible rather than trusting default ordering/timeouts during a real
# outage. `qm shutdown` uses the QEMU guest agent for a clean in-guest
# shutdown when the VM config has `agent: 1` set (confirm with
# `qm config <vmid> | grep ^agent` — the agent being installed in the
# guest isn't enough on its own, Proxmox also has to be told to use it);
# it falls back to ACPI shutdown if the agent isn't reachable.
#
# Fires all VM shutdowns in parallel (they don't depend on each other),
# waits up to WAIT_SECS total for them to finish, then powers the host off
# either way — a stuck VM should not prevent the host (and the rest of
# your fleet's runway) from shutting down cleanly.

WAIT_SECS=180

VMIDS=$(qm list 2>/dev/null | awk 'NR>1 && $3=="running" {print $1}')

if [ -n "$VMIDS" ]; then
    logger -t nut-shutdown "shutting down VMs: $VMIDS"
    for vmid in $VMIDS; do
        qm shutdown "$vmid" --timeout "$WAIT_SECS" --skiplock &
    done

    waited=0
    while [ "$waited" -lt "$WAIT_SECS" ]; do
        still_running=$(qm list 2>/dev/null | awk 'NR>1 && $3=="running" {print $1}')
        [ -z "$still_running" ] && break
        sleep 5
        waited=$((waited + 5))
    done

    still_running=$(qm list 2>/dev/null | awk 'NR>1 && $3=="running" {print $1}')
    if [ -n "$still_running" ]; then
        logger -t nut-shutdown "VM(s) still running after ${WAIT_SECS}s, powering off host anyway: $still_running"
    else
        logger -t nut-shutdown "all VMs shut down cleanly"
    fi
fi

/sbin/shutdown -h +0 "NUT: megarevo battery low, shutting down"
