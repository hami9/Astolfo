#!/usr/bin/env bash
# Launcher for Replit and other managed hosts: prepare a virtualenv, then run.
# A virtualenv is used because many managed Python installs are marked
# externally-managed (PEP 668), which makes a plain `pip install` fail.
set -euo pipefail

PY="${PYTHONBIN:-python3}"
VENV="${VENV_DIR:-.venv}"
STAMP="$VENV/.deps_installed"

if [ ! -x "$VENV/bin/python" ]; then
  echo "creating virtualenv in $VENV..."
  "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"

if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "installing dependencies..."
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "warning: ffmpeg not found, voice and video analysis will be limited"
fi

exec "$VPY" main.py
