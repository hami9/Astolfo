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


def test_env_example_names_every_setting() -> None:
    """A setting nobody can find is a setting nobody uses."""
    from dataclasses import fields

    from astolfo.config import Settings

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = {f.metadata["env"] for f in fields(Settings) if f.metadata.get("env")}
    missing = {name for name in declared if f"{name}=" not in example}
    assert not missing, f".env.example does not mention {sorted(missing)}"


def test_env_example_agrees_with_the_defaults() -> None:
    """An example that contradicts the code teaches the wrong value.

    Only lines that are actually set are checked: a commented-out line is an
    illustration, and a few settings are deliberately shown with a placeholder.
    """
    from dataclasses import fields

    from astolfo.config import Settings, _coerce

    shown = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        shown[name.strip()] = value.split("#")[0].strip()

    defaults = Settings()
    placeholders = {"TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY"}
    wrong = []
    for f in fields(Settings):
        name = f.metadata.get("env")
        if name not in shown or name in placeholders:
            continue
        # Read the example line the way the bot itself would, so 0.30 and 0.3 agree.
        written = _coerce(str(f.type), shown[name])
        actual = getattr(defaults, f.name)
        if written != actual:
            wrong.append(f"{name}: example says {written!r}, code says {actual!r}")
    assert not wrong, wrong


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
