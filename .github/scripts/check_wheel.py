#!/usr/bin/env python3
"""Every package in the source tree must be inside the built wheel.

`astolfo.admin` was once left out because the package list was written by hand,
and the installed bot failed at import. This runs against dist/ in the release
workflow so that can never ship again.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source_packages() -> set[str]:
    return {
        str(path.parent.relative_to(ROOT)).replace("/", ".")
        for path in (ROOT / "astolfo").rglob("__init__.py")
    }


def wheel_packages(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return {
            name.rsplit("/", 1)[0].replace("/", ".")
            for name in archive.namelist()
            if name.endswith("/__init__.py")
        }


def main() -> int:
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    if not wheels:
        raise SystemExit("no wheel in dist/")
    for wheel in wheels:
        missing = source_packages() - wheel_packages(wheel)
        if missing:
            print(f"{wheel.name} is missing {sorted(missing)}", file=sys.stderr)
            return 1
        print(f"{wheel.name}: {len(wheel_packages(wheel))} packages, none missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
