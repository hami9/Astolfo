"""Reading the log from the panel, when a page is not enough.

The screen showed the last twenty-five lines cut to three thousand characters -
a page and a half - and the interesting part of a log is always what came before
the line you can see.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astolfo import server_ops
from astolfo.admin import server as server_screen
from astolfo.admin.panel import Ctx
from tests.conftest import FakeBot

# Numbered so a page can be identified by what is in it.
LINES = [f"line {n}" for n in range(1, 201)]


@pytest.fixture
def ctx(rt):
    return Ctx(rt=rt, user=SimpleNamespace(id=1), bot=FakeBot())


@pytest.fixture
def journalled(monkeypatch):
    """A fake journalctl: the last N lines, newest last, like the real one."""
    seen: list[list[str]] = []

    def fake_run(args, timeout=0.0):
        seen.append(args)
        wanted = int(args[args.index("-n") + 1])
        rows = [row for row in LINES if "-p" not in args or row.endswith(("3", "7"))]
        return True, "\n".join(rows[-wanted:])

    monkeypatch.setattr(server_ops, "_run", fake_run)
    return seen


def test_a_page_is_bigger_than_it_was(journalled) -> None:
    page = server_ops.journal()
    assert len(page.splitlines()) == server_ops.SCREEN_LINES
    assert server_ops.SCREEN_LINES > 25, "the old screen showed 25"
    assert page.endswith("line 200"), "newest last"


def test_paging_back_reaches_what_came_before(journalled) -> None:
    """Chasing a bug means reading what led to it."""
    newest = server_ops.journal()
    older = server_ops.journal(skip=server_ops.SCREEN_LINES)

    assert "line 200" in newest and "line 200" not in older
    assert older.endswith(f"line {200 - server_ops.SCREEN_LINES}")


def test_paging_past_the_beginning_says_so_rather_than_repeating(journalled) -> None:
    assert server_ops.journal(skip=100_000) == "(nothing that far back)"


def test_errors_only_asks_journalctl_for_them(journalled) -> None:
    server_ops.journal(errors_only=True)
    assert journalled[-1][-2:] == ["-p", "3"], journalled[-1]


def test_a_log_that_cannot_be_read_says_so_once(monkeypatch) -> None:
    monkeypatch.setattr(server_ops, "_run", lambda *a, **k: (False, ""))
    assert server_ops.journal() == server_ops.UNREADABLE
    assert server_ops.journal_file("/tmp/never-written.log") == ""


def test_the_whole_thing_can_be_written_to_a_file(journalled, tmp_path) -> None:
    """A message holds about four thousand characters; a document holds the lot."""
    path = server_ops.journal_file(str(tmp_path / "astolfo.log"))

    assert path
    written = (tmp_path / "astolfo.log").read_text()
    assert len(written.splitlines()) == len(LINES), "not truncated to a screenful"


# -- the screen ------------------------------------------------------------
def _targets(view):
    return [b.callback_data for row in view.markup.inline_keyboard for b in row]


def test_the_screen_offers_older_newer_errors_and_a_file(ctx, journalled) -> None:
    targets = _targets(server_screen.log(ctx))

    assert any(t.endswith(f":log:0:{server_ops.SCREEN_LINES}") for t in targets), "older"
    assert any(t.endswith(":log:1:0") for t in targets), "errors only"
    assert any(t.endswith(":logfile:0") for t in targets), "as a file"


def test_the_newer_button_cannot_page_past_the_end(ctx, journalled) -> None:
    targets = _targets(server_screen.log(ctx, skip=0))
    assert any(t.endswith(":log:0:0") for t in targets), targets


def test_the_screen_says_which_page_it_is_showing(ctx, journalled) -> None:
    assert "lines back" in server_screen.log(ctx, skip=40).text
    assert "errors only" in server_screen.log(ctx, errors_only=True).text


def test_asking_for_a_file_attaches_one(ctx, journalled) -> None:
    view = server_screen.log_file(ctx)

    assert view.document.endswith("astolfo.log")
    assert view.alert == "sent as a file"


def test_an_errors_file_is_named_apart_from_the_full_one(ctx, journalled) -> None:
    assert server_screen.log_file(ctx, errors_only=True).document.endswith("astolfo-errors.log")


def test_a_file_that_cannot_be_read_does_not_pretend_it_sent_one(ctx, monkeypatch) -> None:
    monkeypatch.setattr(server_ops, "_run", lambda *a, **k: (False, ""))
    view = server_screen.log_file(ctx)

    assert not view.document
    assert "not readable" in view.alert
