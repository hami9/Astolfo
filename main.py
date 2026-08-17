"""نقطهٔ شروع ربات آستولفو.

اجرا:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="..."      # یا Secrets در Replit
    export OPENROUTER_API_KEY="..."
    python main.py
"""

from __future__ import annotations

import logging
import os

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
    datefmt="%H:%M:%S",
    level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

from astolfo.app import run  # noqa: E402

if __name__ == "__main__":
    run()
