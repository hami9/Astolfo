"""What kind of day the bot is having, decided by the bot.

Step 4 of the brain. It rides on the summary call that already runs every dozen
turns, so it costs no request of its own, and it is deliberately shallow: a mood
tilts the delivery and nothing else. Every mood still renders above the locked
layers, so the worst one is short and dry, never cruel.
"""

from __future__ import annotations

from astolfo import persona
from astolfo.learning import MOOD_LASTS, Mood


def test_it_starts_bright_and_stores_nothing() -> None:
    mood = Mood()
    assert mood.now(at=0) == persona.BRIGHT
    assert mood.dumps() == "", "bright is the resting state, not a stored value"


def test_a_mood_the_bot_chose_holds_then_fades() -> None:
    mood = Mood()
    assert mood.learn("prickly", at=0)

    assert mood.now(at=0) == "prickly"
    assert mood.now(at=MOOD_LASTS - 1) == "prickly"
    assert mood.now(at=MOOD_LASTS) == persona.BRIGHT, "always on its way back"


def test_a_mood_that_does_not_exist_is_dropped() -> None:
    """This arrives as free text from a model, so the name is looked up in a
    fixed table rather than trusted."""
    mood = Mood()
    for junk in ("MURDEROUS", "", "furious", "<script>", "bright bright"):
        assert not mood.learn(junk, at=0), junk
    assert mood.now(at=0) == persona.BRIGHT


def test_every_mood_the_table_names_can_be_chosen() -> None:
    for name in persona.MOODS:
        mood = Mood()
        mood.learn(name, at=0)
        assert mood.now(at=0) == name


def test_choosing_bright_clears_a_mood_rather_than_holding_one() -> None:
    mood = Mood()
    mood.learn("prickly", at=0)
    assert mood.learn(persona.BRIGHT, at=10)

    assert mood.now(at=10) == persona.BRIGHT
    assert mood.dumps(at=10) == ""


def test_the_same_mood_again_is_not_a_change() -> None:
    """The caller logs on a change, and a mood re-chosen every summary is not news."""
    mood = Mood()
    assert mood.learn("sleepy", at=0)
    assert not mood.learn("sleepy", at=60)


def test_a_mood_survives_the_update_that_earned_it() -> None:
    """It fades in hours and the bot is updated more often than that."""
    mood = Mood()
    mood.learn("teasing", at=1000.0)
    after = Mood.loads(mood.dumps(at=1000.0))

    assert after.now(at=1000.0) == "teasing"
    assert after.now(at=1000.0 + MOOD_LASTS) == persona.BRIGHT


def test_a_corrupted_row_costs_the_mood_and_nothing_else() -> None:
    for raw in ("", None, "not json", "[]", '{"name": "prickly"}', '{"name": 5, "since": 0}'):
        assert Mood.loads(raw).now(at=0) == persona.BRIGHT, raw


def test_a_stored_mood_that_has_already_faded_comes_back_bright() -> None:
    stored = '{"name": "prickly", "since": 0}'
    assert Mood.loads(stored).now(at=MOOD_LASTS + 1) == persona.BRIGHT


def test_the_panel_can_read_it() -> None:
    mood = Mood()
    assert mood.summary(at=0) == "bright"
    mood.learn("soft", at=0)
    assert "soft" in mood.summary(at=0)


def test_the_summary_call_is_told_to_choose_one() -> None:
    """It rides on the call that already runs; nothing extra is spent on it."""
    from astolfo.memory import SUMMARY_PROMPT

    assert '"mood"' in SUMMARY_PROMPT
    for name in persona.MOODS:
        assert name in SUMMARY_PROMPT, name


def test_a_mood_never_reaches_past_the_locked_layers() -> None:
    """The floor under every mood: rendered, the constitution is still all there."""
    from astolfo.recipes import FACTORY_LAYERED

    for name in persona.MOODS:
        rendered = FACTORY_LAYERED.render() if name == persona.BRIGHT else None
        if rendered is None:
            from dataclasses import replace

            rendered = replace(FACTORY_LAYERED, mood=name).render()
        for locked in ("identity", "never", "boundaries", "spine", "truth"):
            assert persona.LOCKED[locked] in rendered, (name, locked)


def test_a_mood_older_than_it_lasts_is_not_written_back() -> None:
    """Storing a faded mood would revive it for another four hours on restart."""
    mood = Mood()
    mood.learn("prickly", at=0)

    assert mood.dumps(at=MOOD_LASTS + 1) == ""
