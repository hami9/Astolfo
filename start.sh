#!/usr/bin/env bash
# راه‌انداز Replit: نصب وابستگی‌ها (فقط بار اول) و اجرای ربات.
set -euo pipefail

PY="${PYTHONBIN:-python3}"
STAMP=".deps_installed"

if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "→ نصب وابستگی‌ها..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "⚠️  ffmpeg پیدا نشد — تحلیل ویس و ویدیو محدود می‌شود (به replit.nix اضافه شده است)."
fi

exec "$PY" main.py
