#!/usr/bin/env python3
"""Print the CHANGELOG section for one version, for the release body.

    python .github/scripts/release_notes.py 2.0.0

Fails loudly rather than publishing an empty release: a version with no entry is
a version nobody wrote down.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADING = re.compile(r"^## \[([^\]]+)\]")


def section(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match is None:
            continue
        if start is not None:  # the next version heading ends the section
            return "\n".join(lines[start:index]).strip()
        if match.group(1) == version:
            start = index + 1
    if start is None:
        raise SystemExit(f"no CHANGELOG entry for {version}")
    return "\n".join(lines[start:]).strip()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: release_notes.py <version>")
    version = sys.argv[1].lstrip("v")
    body = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    # Link references live at the bottom of the file, so a trailing one belongs
    # to the oldest section rather than to this one.
    body = re.sub(r"\n\[[^\]]+\]: \S+", "", body).strip()
    if not body:
        raise SystemExit(f"the CHANGELOG entry for {version} is empty")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
