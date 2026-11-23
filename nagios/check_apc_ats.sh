#!/bin/sh
# Nagios active check for an APC Rack ATS (e.g. AP7732) over SNMPv1.
# Usage: check_apc_ats.sh <host> <community>
# Reports which source is feeding the output (A=inverter/Load1, B=mains) and
# whether the switch still has a redundant (failover-capable) second source.
#
# Severity model:
#   OK       - on source A (inverter) and fully redundant: normal.
#   WARNING  - running on source B (mains): the inverter output (A) is down
#              right now; loads are fine on mains, but the inverter isn't
#              supplying. (A brief inverter reboot shows here and clears; the
#              Nagios max_check_attempts soft/hard logic filters the blips.)
#   CRITICAL - on source A but redundancy LOST (mains/B unavailable): the
#              failover path is gone, so the next inverter hiccup drops the
#              load hard. This is the silent-degradation case worth a page.
#   CRITICAL - the ATS itself reports internal comm lost.
#   UNKNOWN  - no SNMP response.
HOST="$1"; COMM="${2:-public}"
OID_COMM=".1.3.6.1.4.1.318.1.1.8.5.1.1.0"   # 1=neverDiscovered 2=established 3=commLost
OID_SRC=".1.3.6.1.4.1.318.1.1.8.5.1.2.0"    # 1=sourceA 2=sourceB
OID_RED=".1.3.6.1.4.1.318.1.1.8.5.1.3.0"    # 1=redundancyLost 2=fullyRedundant

get() { snmpget -v1 -c "$COMM" -OqvU -t2 -r1 "$HOST" "$1" 2>/dev/null; }
C=$(get "$OID_COMM"); S=$(get "$OID_SRC"); R=$(get "$OID_RED")

if [ -z "$C" ] || [ -z "$S" ] || [ -z "$R" ]; then
    echo "UNKNOWN: no SNMP response from ATS $HOST"; exit 3
fi
case "$S" in 1) SRC="A (Inverter/Load1)";; 2) SRC="B (Mains)";; *) SRC="unknown($S)";; esac
case "$R" in 1) RED="redundancy LOST";; 2) RED="fully redundant";; *) RED="unknown($R)";; esac
PERF="|selected_source=$S;;;1;2 redundancy=$R;;;1;2 comm=$C"

if [ "$C" = "3" ]; then
    echo "CRITICAL: ATS reports internal comm lost${PERF}"; exit 2
fi
if [ "$S" = "2" ]; then
    echo "WARNING: running on source $SRC - inverter output (A) is down${PERF}"; exit 1
fi
if [ "$S" = "1" ] && [ "$R" = "1" ]; then
    echo "CRITICAL: on source $SRC but $RED - mains (B) unavailable, NO failover if the inverter drops${PERF}"; exit 2
fi
if [ "$S" = "1" ] && [ "$R" = "2" ]; then
    echo "OK: on source $SRC, $RED${PERF}"; exit 0
fi
echo "UNKNOWN: unexpected state source=$S redundancy=$R${PERF}"; exit 3
