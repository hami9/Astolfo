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


def test_the_brain_section_reads_on_a_build_with_one_and_on_a_build_without(loaded) -> None:
    """The same module is written on both branches. Where there is a bandit it
    reports what it has learned; where there is not it says so rather than
    raising, because a report that fails whole is worse than one with a gap."""
    section = diagnostics.report(loaded).split("brain\n" + "=" * 62)[-1]

    if getattr(loaded, "brain", None) is None:
        assert "no brain" in section
    else:
        assert "selecting" in section, section[:200]


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


def _outcome(db, *, model, variant, day, calls, broken, mode="fast"):
    db.execute(
        "INSERT INTO outcomes (day, service, model, variant, mode, calls, answered,"
        " repaired, broken) VALUES (?, 'openrouter', ?, ?, ?, ?, ?, 0, ?)",
        (day, model, variant, mode, calls, calls - broken, broken),
    )


def test_prompt_weight_folds_a_model_together_across_days_and_modes(rt):
    _outcome(rt.db, model="command-r", variant="compact", day="2026-09-05", calls=60, broken=18)
    _outcome(rt.db, model="command-r", variant="compact", day="2026-09-06", calls=40, broken=12)
    _outcome(
        rt.db, model="command-r", variant="compact", day="2026-09-06",
        calls=10, broken=3, mode="think",
    )

    rows = rt.db.outcomes_by_variant()

    assert len(rows) == 1, "one model on one weight is one row, whatever the day or mode"
    assert rows[0]["calls"] == 110 and rows[0]["broken"] == 33


def test_prompt_weight_refuses_a_verdict_below_the_sample_floor(rt):
    from astolfo.brain import ENOUGH

    _outcome(rt.db, model="command-r", variant="compact", day="2026-09-05", calls=108, broken=33)
    _outcome(rt.db, model="command-r", variant="tight", day="2026-09-06", calls=7, broken=3)

    text = diagnostics.report(rt)

    assert "not a comparison yet" in text
    assert f"tight has 7 of {ENOUGH}" in text
    assert "beats" not in text.split("prompt weight")[1]


def test_prompt_weight_names_the_winner_once_both_arms_have_evidence(rt):
    _outcome(rt.db, model="command-r", variant="compact", day="2026-09-05", calls=100, broken=30)
    _outcome(rt.db, model="command-r", variant="tight", day="2026-09-06", calls=100, broken=10)

    text = diagnostics.report(rt)

    assert "tight beats compact by 20 points" in text


def test_prompt_weight_never_compares_two_different_models(rt):
    """The trap: gemini on tight against command-r on compact measures the models."""
    _outcome(rt.db, model="command-r", variant="compact", day="2026-09-05", calls=100, broken=30)
    _outcome(
        rt.db, model="gemini-flash-lite", variant="tight", day="2026-09-06",
        calls=100, broken=3,
    )

    text = diagnostics.report(rt)

    assert "beats" not in text, "each model had only one weight, so there is nothing to compare"


def test_the_key_count_includes_one_that_came_from_the_env(rt) -> None:
    """A key in `.env` has no database row.

    Counting only rows reported "openrouter 1/1" for a service holding two, and
    the one it hid was the one serving the traffic - which is the exact question
    this column was added to answer.
    """
    from types import SimpleNamespace

    from astolfo.providers import Credential

    rt.llm.providers = [
        SimpleNamespace(
            name="openrouter",
            credentials=[
                Credential(value="stored", id=1),
                Credential(value="from-the-env", label="from .env"),
            ],
        )
    ]
    rt.db.save_service("openrouter", enabled=1)

    text = diagnostics.report(rt)

    assert "2/2" in text, "the key from .env was not counted"
