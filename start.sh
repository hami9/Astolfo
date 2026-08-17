#!/usr/bin/env bash
# Replit launcher: install dependencies once, then run the bot.
set -euo pipefail

PY="${PYTHONBIN:-python3}"
STAMP=".deps_installed"

if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "installing dependencies..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "warning: ffmpeg not found, voice and video analysis will be limited"
fi

exec "$PY" main.py
