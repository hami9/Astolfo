"""Entry point.

    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=... OPENROUTER_API_KEY=...
    python main.py
"""

from astolfo.app import run
from astolfo.logging_setup import configure

if __name__ == "__main__":
    configure()
    run()
