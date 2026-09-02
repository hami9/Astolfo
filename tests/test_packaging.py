"""What has to be true before a version can be tagged.

The wheel once shipped without `astolfo.admin`, so an installed bot failed at
import while the tests were green. These check the parts of packaging and
releasing that no other test would notice.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

import astolfo

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
HEADING = re.compile(r"^## \[([^\]]+)\]", re.M)


def _release_notes():
    """Import the release script by path; .github is not an importable package."""
    path = ROOT / ".github" / "scripts" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


def test_the_version_is_a_version() -> None:
    assert VERSION.match(astolfo.__version__)


def test_the_version_is_single_sourced() -> None:
    """pyproject reads it from the package, so the two can never disagree."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "astolfo.__version__" }' in pyproject


def test_every_package_is_declared() -> None:
    """A package the build cannot find is a package the install will not have."""
    patterns = re.search(
        r"\[tool\.setuptools\.packages\.find\][^\[]*include\s*=\s*\[([^\]]*)\]",
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert patterns, "no packages.find include list in pyproject.toml"
    globs = [item.strip().strip('"').strip("'") for item in patterns.group(1).split(",")]
    globs = [item for item in globs if item]

    packages = {
        str(path.parent.relative_to(ROOT)).replace("/", ".")
        for path in (ROOT / "astolfo").rglob("__init__.py")
    }
    assert "astolfo.admin" in packages, "the fixture for this test moved"
    for package in packages:
        assert any(
            re.fullmatch(glob.replace(".", r"\.").replace("*", ".*"), package)
            for glob in globs
        ), f"{package} matches none of {globs}"


def test_the_changelog_documents_this_version() -> None:
    notes = _release_notes()
    body = notes.section(
        (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), astolfo.__version__
    )
    assert body.strip(), f"CHANGELOG has no entry for {astolfo.__version__}"


def test_the_newest_release_in_the_changelog_is_this_version() -> None:
    """Unreleased may sit on top; the newest numbered entry is what ships."""
    headings = HEADING.findall((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    numbered = [name for name in headings if VERSION.match(name)]
    assert numbered, "the changelog has no numbered release"
    assert numbered[0] == astolfo.__version__


@pytest.mark.parametrize("version", ["9.9.9", "nope"])
def test_a_missing_changelog_entry_is_an_error(version: str) -> None:
    """The release workflow must fail loudly rather than publish empty notes."""
    notes = _release_notes()
    with pytest.raises(SystemExit):
        notes.section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)


def test_the_console_script_reports_the_version(capsys) -> None:
    from astolfo.__main__ import main

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"astolfo {astolfo.__version__}"
