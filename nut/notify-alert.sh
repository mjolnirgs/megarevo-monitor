#!/bin/sh
# /usr/local/bin/notify-alert.sh — NUT NOTIFYCMD hook for the server's
# PRIMARY monitor (upsmon-primary.conf). Emails AND Telegrams on every UPS
# state transition NUT tells us about (on battery, back online, low
# battery, FSD, lost/restored comms with upsd). Wired on the primary
# monitor only (not the secondaries), so this fires once per event
# instead of once per host.
#
# Email needs a working local MTA (mail/mailx -> sendmail) — the same one
# Nagios likely already uses for its own notifications on this host, if
# you have Nagios set up.
#
# Telegram shells out to notify_by_telegram.sh (bundled alongside this
# script) — or point TELEGRAM_SCRIPT at your own notifier instead, as
# long as it takes (chat_id, message) as $1/$2. See that script for how
# to get a bot token and chat ID if you don't have one already.
EMAIL_RECIPIENT="CHANGE_ME@example.com"
TELEGRAM_SCRIPT="/usr/local/bin/notify_by_telegram.sh"
TELEGRAM_CHAT_ID="CHANGE_ME"

MSG="$1"
HOST=$(hostname -f 2>/dev/null || hostname)
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')

case "$NOTIFYTYPE" in
    ONBATT)
        SUBJECT="[megarevo] ON BATTERY - outage in progress"
        ;;
    ONLINE)
        SUBJECT="[megarevo] Power restored - back on grid"
        ;;
    LOWBATT)
        SUBJECT="[megarevo] LOW BATTERY warning"
        ;;
    FSD)
        SUBJECT="[megarevo] Forced shutdown (FSD) - battery.charge.low reached, hosts are shutting down"
        ;;
    COMMBAD)
        SUBJECT="[megarevo] Lost communication with upsd - is megarevo-poller running?"
        ;;
    COMMOK)
        SUBJECT="[megarevo] Communication with upsd restored"
        ;;
    SHUTDOWN)
        SUBJECT="[megarevo] This host is shutting down"
        ;;
    *)
        SUBJECT="[megarevo] NUT event: $NOTIFYTYPE"
        ;;
esac

BODY=$(cat <<EOF
Event: $NOTIFYTYPE
Time:  $NOW
Host:  $HOST (primary monitor for the megarevo dummy-ups)

$MSG
EOF
)

# --- Email ---
if [ "$EMAIL_RECIPIENT" != "CHANGE_ME@example.com" ]; then
    echo "$BODY" | mail -s "$SUBJECT" "$EMAIL_RECIPIENT"
fi

# --- Telegram ---
if [ -x "$TELEGRAM_SCRIPT" ] && [ -n "$TELEGRAM_CHAT_ID" ] && [ "$TELEGRAM_CHAT_ID" != "CHANGE_ME" ]; then
    "$TELEGRAM_SCRIPT" "$TELEGRAM_CHAT_ID" "*${SUBJECT}*
${BODY}"
else
    logger -t nut-notify "notify-alert.sh: TELEGRAM_CHAT_ID not set or $TELEGRAM_SCRIPT missing/not executable - Telegram alert skipped for $NOTIFYTYPE"
fi

exit 0
