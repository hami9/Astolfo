"""The brain in the turn loop, and the screen that stops it.

Steps 3 to 5 joined up: the prompt a turn is built from comes from the bandit,
the chat's mood rides on top of it, the outcome goes back in, and the panel can
see all of it and switch it off.
"""

from __future__ import annotations

import pytest

from astolfo import persona, recipes
from astolfo.admin import brain as brain_screen
from astolfo.chat import recipe_for
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update


@pytest.fixture
def panel_ctx(rt):
    """A panel context for the owner, which is all these screens need."""
    from types import SimpleNamespace

    from astolfo.admin.panel import Ctx

    return Ctx(rt=rt, user=SimpleNamespace(id=1), bot=FakeBot())


async def run(rt, message, bot=None):
    from astolfo import chat as chat_mod

    await chat_mod.handle_message(make_update(message), FakeContext(rt, bot or FakeBot()))


def _state(rt, chat_id: int = -100):
    return rt.store.get(chat_id)


# -- what a turn is built from ---------------------------------------------
def test_with_the_brain_off_it_is_the_prompt_the_bot_has_always_used(rt) -> None:
    """The promise the whole design rests on, asserted on the rendered bytes."""
    rt.brain.on = False
    state = _state(rt)

    assert recipe_for(rt, state, "any/model").render() == persona.static_prompt()

    rt.settings = rt.settings.replace(free_mode=True)
    assert recipe_for(rt, state, "any/model").render() == persona.compact_prompt()


def test_a_weight_chosen_by_hand_overrules_the_brain(rt) -> None:
    """A person pressing a button in the panel beats anything learned."""
    rt.brain.on = True
    rt.settings = rt.settings.replace(prompt_tier="tight")

    assert recipe_for(rt, _state(rt), "any/model").base == persona.TIGHT


def test_a_typo_in_the_weight_falls_back_to_the_brain(rt) -> None:
    rt.settings = rt.settings.replace(prompt_tier="tigth")
    assert recipe_for(rt, _state(rt), "any/model") is recipes.FACTORY_FULL


def test_the_chats_mood_rides_on_whatever_was_chosen(rt) -> None:
    """A mood is about this chat; the recipe is about the model. Both apply."""
    state = _state(rt)
    state.mood.learn("prickly")

    chosen = recipe_for(rt, state, "any/model")

    assert chosen.mood == "prickly"
    assert chosen.base == persona.FULL, "the weight is untouched by the mood"
    assert persona.MOODS["prickly"] in chosen.render()


def test_a_bright_chat_renders_exactly_the_factory_prompt(rt) -> None:
    """Bright is the resting state, not a mood to add a line for."""
    assert recipe_for(rt, _state(rt), "any/model").render() == persona.static_prompt()


# -- and what comes back ---------------------------------------------------
async def test_a_real_turn_teaches_the_brain(rt, llm) -> None:
    llm.reply = "ehehe sure, whatever you say~"
    await run(rt, FakeMessage("astolfo what do you think?"))

    assert rt.brain.arms, "the turn was credited to a recipe"
    assert rt.brain.dirty, "and marked for the autosave rather than written now"


async def test_it_learns_with_the_switch_off_too(rt, llm) -> None:
    """Off, this is the control arm the breaker measures against - which is why
    turning it on is not starting from nothing."""
    rt.brain.on = False
    llm.reply = "yahoo~"
    await run(rt, FakeMessage("astolfo hi"))

    assert rt.brain.arms


async def test_a_failed_call_teaches_it_nothing(rt, llm) -> None:
    """Nothing came back, so the prompt is not what is on trial."""
    llm.reply = None
    await run(rt, FakeMessage("astolfo hi"))

    assert not rt.brain.arms


# -- the screen ------------------------------------------------------------
def test_the_screen_says_what_is_on_and_what_is_running(panel_ctx) -> None:
    view = brain_screen.overview(panel_ctx)

    assert "selecting: off" in view.text
    assert "byte for byte" in view.text, "it says plainly that nothing has changed"
    assert "nothing learned yet" in view.text


def test_a_family_with_too_little_evidence_says_so(panel_ctx) -> None:
    brain = panel_ctx.rt.brain
    for _ in range(3):
        brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT,
                   free_mode=True, chars=200)

    assert "still watching, 3/" in brain_screen.overview(panel_ctx).text


def test_a_family_the_breaker_sent_home_is_named(panel_ctx) -> None:
    brain = panel_ctx.rt.brain
    brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT, free_mode=True, chars=200)
    brain.breaker.paused["qwen3"] = 1e12

    assert "sent home by the breaker" in brain_screen.overview(panel_ctx).text


def test_back_to_factory_stops_it_without_erasing_anything(panel_ctx) -> None:
    """The button for "something is wrong and I want today back"."""
    brain = panel_ctx.rt.brain
    brain.on = True
    for _ in range(40):
        brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT,
                   free_mode=True, chars=200)
    held = len(brain.arms)

    brain_screen.home(panel_ctx)

    assert brain.breaker.tripped, "everything is on the factory recipe"
    assert brain.choose(model="x/qwen3-8b", free_mode=True) is recipes.FACTORY_COMPACT
    assert len(brain.arms) == held, "the evidence is still there tomorrow"


def test_forgetting_everything_is_asked_twice(panel_ctx) -> None:
    brain = panel_ctx.rt.brain
    brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT, free_mode=True, chars=200)

    asked = brain_screen.wipe(panel_ctx, confirmed=False)
    assert brain.arms, "nothing gone yet"
    assert "Forget everything" in asked.text

    brain_screen.wipe(panel_ctx, confirmed=True)
    assert not brain.arms
    assert not brain.breaker.families


def test_writing_cannot_be_switched_on_before_selecting(panel_ctx) -> None:
    """Two switches, in order: selection gets trusted before anything writes."""
    view = brain_screen.switch(panel_ctx, "brain_writes", True)

    assert "selecting on first" in view.alert
    assert not panel_ctx.rt.settings.brain_writes


def test_the_switch_is_stored_so_it_survives_a_restart(panel_ctx) -> None:
    brain_screen.switch(panel_ctx, "brain", True)

    assert panel_ctx.rt.db.overrides().get("brain") == "1"


def test_the_screen_is_reachable_from_the_panel(panel_ctx) -> None:
    """The button on the home screen and the route behind it."""
    from astolfo.admin.sections import home

    markup = home(panel_ctx).markup
    targets = [b.callback_data for row in markup.inline_keyboard for b in row]

    assert any(str(t).endswith(":brain") for t in targets), targets
    assert brain_screen.route(panel_ctx, []).text.startswith("🧩 Brain")
