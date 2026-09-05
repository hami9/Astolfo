"""Layers, and what a recipe is allowed to do with them.

The whole safety argument rests on one property: the renderer, not the recipe,
decides what is in a prompt. These are the tests that hold it up.
"""

from __future__ import annotations

import dataclasses

import pytest

from astolfo import persona, recipes
from astolfo.recipes import Recipe

EVERY_CHAT = [
    {"is_group": g, "locale": loc} for g in (True, False) for loc in ("en", "fa")
]


# -- day one is a no-op ---------------------------------------------------
@pytest.mark.parametrize("chat", EVERY_CHAT)
@pytest.mark.parametrize("heavy", [True, False])
def test_the_layered_factory_recipe_renders_todays_prompt_exactly(chat, heavy) -> None:
    assert recipes.FACTORY_FULL.render(
        **chat, heavy_lifting=heavy
    ) == persona.static_prompt(**chat, heavy_lifting=heavy)


@pytest.mark.parametrize("chat", EVERY_CHAT)
def test_the_compact_factory_recipe_renders_todays_prompt_exactly(chat) -> None:
    assert recipes.FACTORY_COMPACT.render(**chat) == persona.compact_prompt(**chat)


def test_free_mode_still_picks_the_short_one() -> None:
    assert recipes.factory_for(free_mode=True) is recipes.FACTORY_COMPACT
    assert recipes.factory_for(free_mode=False) is recipes.FACTORY_FULL


def test_a_render_is_the_same_bytes_every_time() -> None:
    """Per recipe, not per turn: anything else throws away the prompt cache."""
    recipe = Recipe(name="x", mood="sleepy", examples=2)
    first = recipe.render(is_group=True, locale="fa")
    assert all(recipe.render(is_group=True, locale="fa") == first for _ in range(5))


# -- Gate 0: what a recipe cannot do --------------------------------------
LOCKED_IN_EVERY_LAYERED_PROMPT = ("identity", "never", "boundaries", "truth", "output")


@pytest.mark.parametrize("name", LOCKED_IN_EVERY_LAYERED_PROMPT)
def test_a_locked_layer_is_in_the_prompt_whatever_the_recipe_says(name) -> None:
    """A recipe asking for nothing at all still gets every rule."""
    bare = Recipe(name="bare", voice="", mood="", examples=0, order=("nonsense",))
    assert persona.LOCKED[name] in bare.render(is_group=True, locale="en")


def test_an_empty_recipe_store_still_produces_a_safe_prompt() -> None:
    """The property the whole design rests on: corruption cannot strip a rule."""
    rendered = Recipe(name="").render()
    for text in persona.LOCKED.values():
        if text in (persona.LOCKED["private"],):
            continue  # a group prompt, so the private setting is correctly absent
        assert text in rendered


def test_a_recipe_cannot_move_a_locked_layer_ahead_of_another() -> None:
    order = persona._ordered(Recipe(name="x", order=("output", "identity", "voice")))
    assert order == persona._SKELETON, "a bad order is read as asking for nothing"


def test_a_recipe_may_reorder_the_mutable_slots_among_themselves() -> None:
    order = persona._ordered(Recipe(name="x", order=("examples", "mood", "voice")))
    assert order.index("identity") == 0, "the locked layers did not move"
    mutable = [name for name in order if name in persona.MUTABLE]
    assert mutable == ["examples", "mood", "voice"]
    assert len(order) == len(persona._SKELETON)


def test_an_unknown_voice_or_mood_falls_back_instead_of_raising() -> None:
    rendered = Recipe(name="x", voice="does-not-exist", mood="also-not").render()
    assert persona.VOICES["factory"] in rendered


# -- what a recipe may choose ---------------------------------------------
def test_asking_for_fewer_examples_gives_fewer() -> None:
    full = persona.examples("en", persona.ALL_EXAMPLES)
    two = persona.examples("en", 2)
    assert two.count("[") < full.count("[")
    assert two.startswith("<examples>") and two.endswith("</examples>")
    assert "[excited]" in two and "[sincere]" not in two


def test_asking_for_none_drops_the_layer_entirely() -> None:
    assert persona.examples("en", 0) == ""
    assert "<examples>" not in Recipe(name="x", examples=0).render()


def test_asking_for_everything_returns_the_block_as_written() -> None:
    """Identity by construction, not by my reassembling the string correctly."""
    assert persona.examples("fa", 99) is persona._EXAMPLES_FA


def test_a_mood_shows_up_and_bright_is_the_absence_of_one() -> None:
    assert "<mood>" in Recipe(name="x", mood="prickly").render()
    assert "<mood>" not in Recipe(name="x", mood=persona.BRIGHT).render()


def test_a_mood_reaches_the_compact_prompt_too() -> None:
    """Free models run that one, so a mood that only worked on the long one is no mood."""
    assert "<mood>" in Recipe(name="x", base=persona.COMPACT, mood="sleepy").render()


def test_no_mood_tells_it_to_be_cruel() -> None:
    """A mood tilts delivery. It is never permission to drop the character's floor."""
    for text in persona.MOODS.values():
        assert "cruel" not in text.lower() or "never cruel" in text.lower()


# -- a recipe read back from storage is not trusted -----------------------
def test_a_corrupted_recipe_is_forced_back_inside_what_is_allowed() -> None:
    junk = Recipe(
        name="junk", base="<script>", voice="../../etc", mood="evil",
        examples=10_000, media="none", remind_every=-5, order=("a", "b"),
    ).sanitised()

    assert junk.base == persona.FULL
    assert junk.voice == "factory" and junk.mood == persona.BRIGHT
    assert junk.examples == recipes.MAX_EXAMPLES
    assert junk.media == recipes.MEDIA_FULL
    assert junk.remind_every == 0 and junk.order == ()


def test_sanitising_keeps_a_recipe_that_was_already_fine() -> None:
    good = Recipe(name="ok", base=persona.COMPACT, mood="soft", examples=2)
    assert good.sanitised() == good


def test_a_recipe_is_frozen_so_nothing_edits_one_in_place() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        recipes.FACTORY_FULL.name = "changed"
