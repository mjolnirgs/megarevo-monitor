#!/bin/sh
# /usr/local/bin/notify-timed-shutdown.sh — NUT NOTIFYCMD hook for a host
# that sheds early into an outage on a fixed timer, independent of the
# battery's actual SOC — for a host that isn't needed during an outage,
# where shutting it down early buys the rest of your fleet more runway
# off the same battery. The SOC-based SHUTDOWNCMD (OB LB, at/below
# battery_charge_low) is still wired up too, as a safety net in case the
# battery drains faster than this timer's budget assumes.
#
# upsmon calls this with $NOTIFYTYPE set (ONBATT, ONLINE, LOWBATT, FSD, ...)
# and the human-readable message as $1. We only act on ONBATT/ONLINE.
#
# `shutdown -c` cancels a pending scheduled shutdown; it's a no-op
# (harmless exit status aside) if nothing is scheduled, which covers power
# flapping on and off without needing to track state ourselves.

SHUTDOWN_DELAY_MINUTES=60

case "$NOTIFYTYPE" in
    ONBATT)
        /sbin/shutdown -c 2>/dev/null
        /sbin/shutdown -h "+${SHUTDOWN_DELAY_MINUTES}" "NUT: megarevo on battery power - shutting down in ${SHUTDOWN_DELAY_MINUTES}min unless power is restored (sheds early to extend runway for other hosts)"
        ;;
    ONLINE)
        /sbin/shutdown -c 2>/dev/null
        ;;
esac

exit 0
