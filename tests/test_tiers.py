"""Three prompt weights, because one size was measurably the wrong size.

Measured rather than guessed: the full prompt is ~4,600 tokens across 52 separate
rules and the compact one ~1,080 across about thirty. `cohere/command-r-08-2024`
is a 35B model, and an evening of its output shows what thirty rules buys - it
followed some and dropped the rest: the language rule, the one-line rule, the
sincere rule, all gone in the same conversation.

So the weight becomes a choice, and every rule dropped from the lightest one is
still enforced in code.
"""

from __future__ import annotations

from astolfo import persona
from astolfo.chat import prompt_variant, static_block_for
from astolfo.config import Settings


def _at(tier: str) -> str:
    return static_block_for(tier, is_group=True, locale="en", heavy_lifting=False)


# -- the weights themselves ------------------------------------------------
def test_each_weight_is_actually_lighter_than_the_last() -> None:
    tight, compact, full = (len(_at(t)) for t in persona.TIERS)

    assert tight < compact < full
    assert tight * 3 < compact, "a third of the weight, not a trim"


def test_the_lightest_one_is_small_enough_to_be_worth_having() -> None:
    """~300 tokens. Past about 500 a 7B model is back to dropping rules."""
    assert len(persona.tight_prompt()) < 1600


def test_the_rules_whose_absence_does_real_damage_are_all_there() -> None:
    """What survived the cut, and why each one did."""
    tight = " ".join(persona.tight_prompt().lower().split())

    assert "one message as yourself" in tight, "it continued the transcript"
    assert "language of the newest message" in tight, "it answered Persian in English"
    assert "one short line" in tight, "it wrote paragraphs"
    assert "never invent" in tight, "it made things up about people"
    assert "nothing sexual" in tight, "the ladder from 2.5.2"
    assert "never agree with it" in tight, "the doormat from 2.6.6"
    assert "genuinely hurting" in tight, "the one it missed that mattered"


def test_the_voice_comes_from_the_example_at_this_weight() -> None:
    """Fourteen lines of voice rules do not fit; one sample does the work."""
    for locale, line in (("en", "took you three days to notice"), ("fa", "سه روز طول کشید")):
        assert line in persona.tight_prompt(locale=locale)


def test_every_weight_knows_whether_it_is_a_group() -> None:
    """The full prompt has a whole block for it; the lighter two have one line."""
    for tier in persona.TIERS:
        private = static_block_for(tier, is_group=False, locale="en", heavy_lifting=False)
        group = static_block_for(tier, is_group=True, locale="en", heavy_lifting=False)

        assert "private" in private.lower(), tier
        assert private != group, tier


# -- choosing one ----------------------------------------------------------
def test_auto_is_what_the_bot_has_always_done() -> None:
    """The default changes nothing: compact on free models, full otherwise."""
    settings = Settings(telegram_token="t", api_key="k")

    assert prompt_variant(settings.replace(free_mode=True)) == persona.COMPACT
    assert prompt_variant(settings.replace(free_mode=False)) == persona.FULL


def test_a_chosen_weight_beats_free_mode() -> None:
    """The point of the setting: free mode no longer decides this on its own."""
    settings = Settings(telegram_token="t", api_key="k", free_mode=True)

    assert prompt_variant(settings.replace(prompt_tier="tight")) == persona.TIGHT
    assert prompt_variant(settings.replace(prompt_tier="full")) == persona.FULL


def test_a_typo_falls_back_rather_than_sending_something_odd() -> None:
    settings = Settings(telegram_token="t", api_key="k", free_mode=True, prompt_tier="tigth")

    assert prompt_variant(settings) == persona.COMPACT


def test_the_panel_can_reach_it() -> None:
    from astolfo.admin.sections import COMMON

    assert "prompt_tier" in COMMON


# -- what the lightest prompt drops is still enforced ----------------------
def test_the_guards_do_not_depend_on_which_prompt_produced_the_reply() -> None:
    """The argument for cutting rules: the code checks run either way. If these
    ever became tier-dependent, the lightest weight would stop being safe."""
    from astolfo.text import cut_impersonation, looks_broken, strip_speaker, went_explicit

    assert went_explicit("آره، کص میخوام.")
    assert looks_broken("assistant: hello") == "answered in transcript format"
    assert strip_speaker("Astolfo: hi", ["Astolfo"]) == "hi"
    assert cut_impersonation("hi\nReza: no", ["Reza"]) == "hi"
