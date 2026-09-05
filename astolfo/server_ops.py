"""Reading the health of the machine, and asking for the two jobs that need root.

The bot runs unprivileged, so it cannot restart or update itself. Instead of
handing it sudo, it writes one word into a spool file that a root helper watches
(`deploy/astolfo-agent.sh`). The helper accepts nothing but the words below, so
no text from Telegram is ever passed to a shell.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

CONTROL_DIR = "control"
REQUEST_FILE = "request"
RESULT_FILE = "result"

# The entire vocabulary the privileged side understands.
ACTIONS = ("restart", "update")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = "astolfo"


def _run(argv: list[str], timeout: float = 15.0) -> tuple[bool, str]:
    """Run a fixed command. No shell, no arguments from anywhere but this file."""
    try:
        done = subprocess.run(  # noqa: S603 - argv is a literal list, shell=False
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (done.stdout or done.stderr or "").strip()
    return done.returncode == 0, output


def _git(*args: str, timeout: float = 20.0) -> tuple[bool, str]:
    return _run(["git", "-C", REPO_ROOT, *args], timeout=timeout)


def commit() -> str:
    ok, text = _git("log", "--oneline", "-1")
    return text if ok else "unknown"


def branch() -> str:
    ok, text = _git("rev-parse", "--abbrev-ref", "HEAD")
    return text if ok else "?"


def updates_available() -> tuple[int, str]:
    """How many commits the server is behind, after asking the remote."""
    fetched, detail = _git("fetch", "--quiet", "origin", branch(), timeout=45.0)
    if not fetched:
        return 0, f"could not reach the remote: {detail[:120]}"
    ok, text = _git("rev-list", "--count", f"HEAD..origin/{branch()}")
    if not ok or not text.isdigit():
        return 0, "could not compare with the remote"
    count = int(text)
    return count, "up to date" if count == 0 else f"{count} commit(s) behind"


def uptime() -> str:
    try:
        with open("/proc/uptime") as fh:
            seconds = float(fh.read().split()[0])
    except (OSError, ValueError):
        return "?"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}d {hours}h {rest // 60}m" if days else f"{hours}h {rest // 60}m"


def memory() -> tuple[int, int]:
    """Used and total megabytes, or zeros where /proc is not available."""
    values = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        return 0, 0
    total = values.get("MemTotal", 0)
    return total - values.get("MemAvailable", total), total


def disk(path: str = "/") -> tuple[int, int]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0, 0
    return usage.used // 1_000_000_000, usage.total // 1_000_000_000


def load() -> str:
    try:
        return ", ".join(f"{value:.2f}" for value in os.getloadavg())
    except OSError:
        return "?"


def service_state() -> str:
    ok, text = _run(["systemctl", "is-active", SERVICE], timeout=5.0)
    return text or ("active" if ok else "unknown")


# What fits in one Telegram message, and what fits in a file. The screen used to
# show twenty-five lines cut to three thousand characters, which is a page and a
# half of a log - not enough to see what led to anything.
SCREEN_LINES = 40
SCREEN_CHARS = 3400
FILE_LINES = 3000

UNREADABLE = "the log is not readable from here (the service user needs systemd-journal)"


def journal(lines: int = SCREEN_LINES, *, errors_only: bool = False, skip: int = 0) -> str:
    """The last few lines of the service log, newest last.

    `skip` pages backwards: a page is `lines` long, so skip=40 is the forty lines
    before the forty you just read. Chasing a bug means reading what came before
    it, and before this the screen could only ever show the end.
    """
    wanted = ["journalctl", "-u", SERVICE, "--no-pager", "--output", "cat",
              "-n", str(lines + max(0, skip))]
    if errors_only:
        # priority 3 and above: err, crit, alert, emerg. Warnings are noisy and
        # the interesting ones are logged as errors anyway.
        wanted += ["-p", "3"]
    ok, text = _run(wanted, timeout=15.0)
    if not ok:
        return UNREADABLE
    rows = text.splitlines()
    if skip:
        rows = rows[: len(rows) - skip] if len(rows) > skip else []
    return "\n".join(rows[-lines:])[-SCREEN_CHARS:] or "(nothing that far back)"


def journal_file(path: str, *, errors_only: bool = False) -> str:
    """The whole recent log written to a file, for reading somewhere with room.

    A Telegram message holds about four thousand characters; a document holds as
    much as anybody needs. Returns the path, or "" when the log cannot be read.
    """
    text = journal(FILE_LINES, errors_only=errors_only)
    if text == UNREADABLE:
        return ""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        log.warning("could not write the log file: %s", exc)
        return ""
    return path


# -- asking for a privileged job ------------------------------------------
def _control_path(data_dir: str, name: str) -> str:
    return os.path.join(data_dir, CONTROL_DIR, name)


def agent_installed(data_dir: str) -> bool:
    return os.path.isdir(os.path.join(data_dir, CONTROL_DIR))


def request(data_dir: str, action: str) -> tuple[bool, str]:
    """Leave one word for the root helper to pick up."""
    if action not in ACTIONS:
        return False, f"{action} is not something the helper can do"

    directory = os.path.join(data_dir, CONTROL_DIR)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(_control_path(data_dir, REQUEST_FILE), "w", encoding="utf-8") as fh:
            fh.write(action)
    except OSError as exc:
        return False, f"could not reach the helper: {exc}"

    log.info("asked the helper to %s", action)
    return True, f"asked the server to {action}"


def last_result(data_dir: str) -> str:
    try:
        with open(_control_path(data_dir, RESULT_FILE), encoding="utf-8") as fh:
            return fh.read().strip()[-800:]
    except OSError:
        return ""


def result_age(data_dir: str) -> float:
    try:
        return time.time() - os.path.getmtime(_control_path(data_dir, RESULT_FILE))
    except OSError:
        return float("inf")
