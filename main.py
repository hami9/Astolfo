"""Entry point for running from a clone.

    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=... OPENROUTER_API_KEY=...
    python main.py

Installed from a package, the same thing is spelled `astolfo`.
"""

from astolfo.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
