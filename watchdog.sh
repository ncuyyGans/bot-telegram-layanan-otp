#!/bin/bash
# Watchdog otpbot — dipanggil cron tiap 5 menit.
# Menyalakan ulang bot bila prosesnya mati (mis. VM restart).
DIR="$(dirname "$0")"
if ! pgrep -f "[o]tpbot.py" > /dev/null; then
    echo "$(date '+%F %T') watchdog: otpbot mati, menyalakan ulang" >> "$DIR/bot.log"
    nohup "$DIR/run.sh" >> "$DIR/bot.log" 2>&1 &
fi
