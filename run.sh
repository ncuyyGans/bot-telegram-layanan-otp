#!/bin/bash
# Jalankan otpbot. Dijalankan dari direktori mana pun.
cd "$(dirname "$0")" || exit 1
exec python3 otpbot.py
