"""One file with everything a shell on the box would have been used for.

Written because SSH into the server is not available from where this is built,
and every diagnosis so far has gone through somebody copying a screen into a
chat - which loses whatever scrolled away and never includes the tables.

The property that matters most is the last one: it can be pasted anywhere.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from astolfo import diagnostics, faults
from astolfo.admin import sections
from astolfo.admin.panel import Ctx
from tests.conftest import FakeBot

SECRET = "sk-or-v1-000000000000abcd"


@pytest.fixture
def loaded(rt):
    """A runtime with something in every table worth reporting.

    Every table, on purpose. The first version of this fixture left `services`
    and `credentials` empty, so the services section rendered "(nothing yet)"
    and the line that read a column off the wrong table was never executed -
    it took a real database to find it.
    """
    db = rt.db
    db.save_service("cohere", enabled=1)
    db.save_service("google", enabled=1, rested_until=time.time() + 6 * 3600,
                    last_error="google/gemini-flash-latest: HTTP 429 the allowance is spent")
    db.add_credential("cohere", b"ciphertext", label="from .env")
    db.note_strike("command-r7b-12-2024")
    db.add_outcome("2026-09-05", service="cohere", model="command-r7b-12-2024",
                   variant="compact", mode="fast", calls=49, broken=21, repaired=4)
    kept = [(
        time.time() - 240,
        faults.read(
            429,
            '{"message":"You are using a Trial key, which is limited to 20 API calls / minute."}',
            service="cohere", model="command-r7b-12-2024",
        ),
    )]
    rt.llm.recent_faults = lambda service="": kept
    return rt


def test_the_services_section_says_which_are_out_and_why(loaded) -> None:
    """This section crashed on its first real database, which is exactly the one
    a diagnosis needs: every other table says what happened, only this one says
    which services could have answered at all."""
    text = diagnostics.report(loaded)

    assert "could not be read" not in text
    assert "google" in text and "the allowance is spent" in text
    assert "why it is out" in text


def test_it_reports_what_each_model_actually_produced(loaded) -> None:
    """The number the whole argument about a model turns on."""
    text = diagnostics.report(loaded)

    assert "command-r7b-12-2024" in text
    assert "21 (42%)" in text, "broken, as a share of what it was asked"


def test_it_reports_what_a_service_said_in_its_own_words(loaded) -> None:
    text = diagnostics.report(loaded)

    assert "limited to 20 API calls / minute" in text
    assert "too many requests per minute" in text, "and how it was read"


def test_it_reports_the_switches_that_change_behaviour(loaded) -> None:
    loaded.settings = loaded.settings.replace(prompt_tier="tight", free_mode=True)
    text = diagnostics.report(loaded)

    assert "prompt weight   tight" in text
    assert "free mode       on" in text


def test_a_build_without_a_brain_says_so_rather_than_failing(loaded) -> None:
    """The same file is written on both branches; one of them has no bandit."""
    assert "no brain" in diagnostics.report(loaded)


def test_one_unreadable_table_does_not_cost_the_report(loaded) -> None:
    loaded.db.outcomes = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk is full"))
    text = diagnostics.report(loaded)

    assert "could not be read" in text
    assert "model health" in text, "the sections after it are still there"


# -- the property that makes it shareable ----------------------------------
def test_it_carries_no_credential(loaded, monkeypatch) -> None:
    """It is written to be pasted into a chat, so this is the whole point."""
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    loaded.settings = loaded.settings.replace(api_key=SECRET, telegram_token="123:ABC-token")
    text = diagnostics.report(loaded)

    assert SECRET not in text
    assert "123:ABC-token" not in text
    assert "sk-" not in text


def test_it_carries_no_chat_text_and_nobody_s_name(loaded) -> None:
    """The counters are about models and services. The two places a message
    could leak in - the notes and the learned style - are not in it."""
    state = loaded.store.get(-100)
    state.notes = "Reza is getting divorced and asked us not to tell anyone"
    state.style.learn(chat="they write in Finglish", people={"Reza": "asks real questions"})
    loaded.store.save(force=True)

    text = diagnostics.report(loaded)

    assert "divorced" not in text
    assert "Reza" not in text
    assert "Finglish" not in text


# -- the button ------------------------------------------------------------
def test_the_panel_sends_it_as_a_file(loaded) -> None:
    ctx = Ctx(rt=loaded, user=SimpleNamespace(id=1), bot=FakeBot())
    view = sections.diagnostics_report(ctx)

    assert view.document.endswith("astolfo-diagnostics.txt")
    assert view.alert == "sent as a file"
    with open(view.document, encoding="utf-8") as fh:
        assert "Astolfo diagnostics" in fh.read()


def test_a_report_that_cannot_be_written_does_not_pretend(loaded, monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "write", lambda *a, **k: "")
    ctx = Ctx(rt=loaded, user=SimpleNamespace(id=1), bot=FakeBot())
    view = sections.diagnostics_report(ctx)

    assert not view.document
    assert "could not be written" in view.alert


def test_the_button_is_on_the_data_screen(loaded) -> None:
    ctx = Ctx(rt=loaded, user=SimpleNamespace(id=1), bot=FakeBot())
    targets = [
        b.callback_data for row in sections.data(ctx).markup.inline_keyboard for b in row
    ]

    assert any(str(t).endswith(":data:diag") for t in targets), targets
