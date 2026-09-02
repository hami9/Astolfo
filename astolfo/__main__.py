"""The `astolfo` command, and `python -m astolfo`."""

from __future__ import annotations

import sys

from . import __version__
from .app import run
from .logging_setup import configure

USAGE = f"""astolfo {__version__} - a Telegram bot that lives in your group chat

usage: astolfo [--version] [--help]

Configuration is read from the environment and from a .env file beside the
working directory. See https://github.com/hami9/Astolfo for the full list.
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--version" in args or "-V" in args:
        print(f"astolfo {__version__}")
        return 0
    if "--help" in args or "-h" in args:
        print(USAGE, end="")
        return 0

    configure()
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
