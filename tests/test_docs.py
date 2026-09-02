"""The documentation is part of the project, so its links are checked like code.

A public repository is mostly read, not run. A link that points at a file somebody
renamed is the most common way for that reading to go wrong, and it is cheap to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# [text](target), ignoring images and anything with a title after the target.
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")

REQUIRED = [
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "LICENSE",
    ".env.example",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    "docs/ADMIN.md",
    "docs/ARCHITECTURE.md",
    "docs/COST.md",
    "docs/DEPLOYMENT.md",
]


def markdown_files() -> list[Path]:
    return sorted([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")])


@pytest.mark.parametrize("name", REQUIRED)
def test_the_file_the_docs_promise_is_there(name: str) -> None:
    assert (ROOT / name).exists(), f"{name} is referenced but missing"


@pytest.mark.parametrize("page", markdown_files(), ids=lambda p: p.name)
def test_every_relative_link_resolves(page: Path) -> None:
    broken = []
    for target in LINK.findall(page.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path, _, _anchor = target.partition("#")
        if not path:
            continue
        if not (page.parent / path).resolve().exists():
            broken.append(target)
    assert not broken, f"{page.name} links to {broken}"


def test_the_readme_lists_every_module_in_the_package() -> None:
    """The layout section drifts silently otherwise, and it is the first thing read."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    modules = {
        path.name
        for path in (ROOT / "astolfo").glob("*.py")
        if path.name not in {"__init__.py", "__main__.py", "logging_setup.py"}
    }
    missing = {name for name in modules if f"astolfo/{name}" not in readme}
    assert not missing, f"the layout section does not mention {sorted(missing)}"
