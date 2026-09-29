#!/bin/bash
# /usr/local/bin/notify_by_telegram.sh — minimal Telegram sender.
# Args: $1=chat_id $2=message
#
# A reference implementation, not the only way to do this — if you
# already have a Telegram bot/notifier wired up for something else (e.g.
# Nagios itself), point notify-alert.sh's TELEGRAM_SCRIPT at that one
# instead of this, as long as it takes the same (chat_id, message)
# calling convention. If not, this is enough on its own: create a bot via
# @BotFather in Telegram, drop its token below, then message the bot once
# and hit https://api.telegram.org/bot<TOKEN>/getUpdates to find your
# chat_id.
TOKEN="<your-bot-token-here>"
CHAT_ID="$1"
MSG="$2"

curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d chat_id="${CHAT_ID}" \
  -d parse_mode="Markdown" \
  --data-urlencode text="${MSG}" \
  > /dev/null
