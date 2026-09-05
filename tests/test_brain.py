"""The bandit, and the promise that it changes nothing until it is switched on.

Step 3 of the brain: choosing a recipe for the model that is about to answer.
Everything here is arithmetic and counters - no model is called, and the cage in
`guardrail.py` is asked before anything is chosen.
"""

from __future__ import annotations

import random

from astolfo import persona, recipes
from astolfo.brain import (
    ANSWERED,
    CEILING,
    ENOUGH,
    FLOOR,
    Arm,
    Brain,
    family,
    pool,
    reward,
)
from astolfo.guardrail import EXPLORATION_FLOOR, MAX_FAMILIES, MAX_VARIANTS


def _fixed(seed: int = 7) -> Brain:
    return Brain(on=True, rng=random.Random(seed))


# -- the switch, which is the whole safety story ---------------------------
def test_off_by_default() -> None:
    assert Brain().on is False


def test_off_reproduces_todays_prompt_byte_for_byte() -> None:
    """A bug in the bandit cannot reach the chat until somebody presses the
    button. This is the assertion that says so."""
    off = Brain()
    for free_mode, expected in (
        (True, persona.compact_prompt()),
        (False, persona.static_prompt()),
    ):
        chosen = off.choose(model="cohere/command-r-08-2024", free_mode=free_mode)
        assert chosen.render() == expected


def test_a_family_with_nothing_learned_still_gets_the_factory_recipe() -> None:
    brain = _fixed()
    chosen = brain.choose(model="cohere/command-r-08-2024", free_mode=True)

    assert chosen is recipes.FACTORY_COMPACT
    assert chosen.render() == persona.compact_prompt()


# -- families, from the ids the server actually ran ------------------------
def test_a_rename_inherits_what_the_last_one_taught() -> None:
    """Both are the same weights to a prompt, and both really are in the pool."""
    assert family("cohere/command-r-08-2024") == family("cohere/command-r-03-2024")
    assert family("command-r-08-2024") == "command-r"


def test_a_different_model_is_not_the_same_family() -> None:
    assert family("command-r7b-12-2024") != family("command-r-08-2024")
    assert family("google/gemini-2.5-flash") != family("google/gemini-2.5-pro")
    assert family("qwen/qwen3-30b-a3b:free") == "qwen3"
    assert family("minimax/minimax-m3:free") == "minimax"


def test_an_id_nobody_has_seen_still_gets_a_stable_name() -> None:
    """The pool is discovered, so most ids will be ones I never wrote down."""
    first = family("somebody/brand-new-thing-01-2026")
    assert first == family("somebody/brand-new-thing-04-2026"), "a release is not a family"
    assert first and first != "unknown"
    assert family("") == "unknown"


# -- the reward ------------------------------------------------------------
def test_a_short_reply_that_got_answered_wins_nothing() -> None:
    """Without this floor the bot learns that one word provokes "چی؟" and
    converges on being useless."""
    assert reward(answered=True, chars=200) == ANSWERED
    assert reward(answered=True, chars=3) == 0.0


def test_broken_is_worse_than_repaired_is_worse_than_nothing() -> None:
    assert reward(broken=True) < reward(repaired=True) < reward() <= 0.0


def test_a_reply_that_was_repaired_wins_nothing_even_if_answered() -> None:
    assert reward(answered=True, chars=200, repaired=True) < 0


def test_length_costs_something() -> None:
    """A recipe cannot win by writing twice as much."""
    brief = reward(answered=True, chars=200, tokens=50, ceiling=500)
    verbose = reward(answered=True, chars=200, tokens=500, ceiling=500)

    assert brief > verbose


def test_the_reward_stays_inside_its_bounds() -> None:
    worst = reward(broken=True, repaired=True, tokens=9999, ceiling=100)
    assert FLOOR <= worst <= CEILING
    assert reward(answered=True, chars=999) <= CEILING


# -- choosing --------------------------------------------------------------
def test_it_waits_for_enough_evidence() -> None:
    brain = _fixed()
    for _ in range(ENOUGH - 1):
        brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT,
                   free_mode=True, answered=True, chars=200)

    assert brain.choose(model="x/qwen3-8b", free_mode=True) is recipes.FACTORY_COMPACT


def test_a_clearly_better_recipe_wins_most_turns() -> None:
    """Not every turn - the exploration floor spends one in ten elsewhere."""
    brain = _fixed()
    options = pool(free_mode=True)
    good, bad = options[1], options[0]
    for _ in range(60):
        brain.note(model="x/qwen3-8b", recipe=good, free_mode=True, answered=True, chars=200)
        brain.note(model="x/qwen3-8b", recipe=bad, free_mode=True, broken=True)

    picks = [brain.choose(model="x/qwen3-8b", free_mode=True).name for _ in range(200)]
    assert picks.count(good.name) > 150, picks.count(good.name)


def test_a_tenth_of_the_turns_go_off_the_winner() -> None:
    """A stale winner cannot lock a family in for good."""
    brain = _fixed()
    options = pool(free_mode=True)
    for _ in range(80):
        brain.note(model="x/qwen3-8b", recipe=options[1], free_mode=True,
                   answered=True, chars=200)
        brain.note(model="x/qwen3-8b", recipe=options[0], free_mode=True, broken=True)

    picks = [brain.choose(model="x/qwen3-8b", free_mode=True).name for _ in range(400)]
    others = sum(1 for name in picks if name != options[1].name)

    assert others >= 400 * EXPLORATION_FLOOR * 0.5, others


def test_a_paused_family_runs_the_control_arm() -> None:
    """Gate 5, and it is consulted before selection rather than after it."""
    brain = _fixed()
    for _ in range(ENOUGH * 2):
        brain.note(model="x/qwen3-8b", recipe=pool(free_mode=True)[1],
                   free_mode=True, answered=True, chars=200)
    brain.breaker.paused["qwen3"] = 1e12

    assert brain.choose(model="x/qwen3-8b", free_mode=True) is recipes.FACTORY_COMPACT


def test_the_whole_brain_tripping_sends_every_family_home() -> None:
    brain = _fixed()
    for _ in range(ENOUGH * 2):
        brain.note(model="x/qwen3-8b", recipe=pool(free_mode=True)[1],
                   free_mode=True, answered=True, chars=200)
    brain.breaker.tripped = "broken everywhere"

    assert brain.choose(model="x/qwen3-8b", free_mode=True) is recipes.FACTORY_COMPACT


# -- the pool --------------------------------------------------------------
def test_the_factory_recipe_can_never_leave_the_pool() -> None:
    for free_mode in (True, False):
        options = pool(free_mode=free_mode)
        assert options[0] is recipes.factory_for(free_mode=free_mode)
        assert len(options) <= MAX_VARIANTS


def test_every_recipe_in_the_pool_renders_the_same_locked_layers() -> None:
    """The bandit moves mutable fields and nothing else. If it could drop a
    locked layer, none of the rest of the cage would matter."""
    for free_mode in (True, False):
        for recipe in pool(free_mode=free_mode):
            rendered = recipe.render()
            if recipe.short:
                assert persona.short_block(recipe.base) in rendered, recipe.name
            else:
                for name in ("identity", "never", "boundaries", "spine", "truth"):
                    assert persona.LOCKED[name] in rendered, (recipe.name, name)


# -- the bounds ------------------------------------------------------------
def test_the_table_stops_growing_at_the_limit() -> None:
    """A 1 GB box, and the pool is discovered: without a cap this grows weekly."""
    brain = _fixed()
    for n in range(MAX_FAMILIES * 3):
        brain.note(model=f"vendor/madeup{n}", recipe=recipes.FACTORY_COMPACT,
                   free_mode=True, answered=True, chars=200)

    assert len({fam for fam, _ in brain.arms}) <= MAX_FAMILIES


def test_a_family_the_table_had_no_room_for_still_answers() -> None:
    """Refusing to learn is not refusing to reply."""
    brain = _fixed()
    for n in range(MAX_FAMILIES * 2):
        brain.note(model=f"vendor/madeup{n}", recipe=recipes.FACTORY_COMPACT,
                   free_mode=True, answered=True, chars=200)

    assert brain.choose(model="vendor/nowhere-near-the-table", free_mode=True)


# -- carrying it across a restart -----------------------------------------
def test_the_counters_survive_a_round_trip() -> None:
    brain = _fixed()
    for _ in range(5):
        brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT,
                   free_mode=True, answered=True, chars=200)
    rows = brain.rows()

    after = _fixed()
    after.restore(rows)

    assert after.rows() == rows
    assert after.seen("qwen3") == 5


def test_a_corrupted_row_is_dropped_rather_than_raised() -> None:
    """Counters go through JSON and a database, so they are not trusted back."""
    brain = _fixed()
    brain.restore([
        {"family": "qwen3", "recipe": "compact", "wins": 2, "losses": 1, "samples": 3},
        {"family": "qwen3", "recipe": "bad", "wins": "not a number", "losses": 1},
        {"nothing": "useful"},
        None,
    ])

    assert list(brain.arms) == [("qwen3", "compact")]


def test_a_negative_count_cannot_come_back_in() -> None:
    brain = _fixed()
    brain.restore([{"family": "q", "recipe": "r", "wins": -5, "losses": -5, "samples": -5}])
    arm = brain.arms[("q", "r")]

    assert (arm.wins, arm.losses, arm.samples) == (0.0, 0.0, 0)


# -- the arm itself --------------------------------------------------------
def test_an_arm_is_a_pair_of_counts_and_nothing_more() -> None:
    arm = Arm()
    arm.note(CEILING)
    arm.note(FLOOR)

    assert arm.samples == 2
    assert abs(arm.wins + arm.losses - 2.0) < 1e-9, "each turn is worth exactly one"
    assert abs(arm.mean - 0.5) < 1e-9, "one best and one worst average out"


def test_a_broken_turn_moves_an_arm_the_right_way() -> None:
    good, bad = Arm(), Arm()
    for _ in range(10):
        good.note(reward(answered=True, chars=200))
        bad.note(reward(broken=True))

    assert good.mean > bad.mean


def test_counters_are_marked_for_the_autosave_not_written_per_turn() -> None:
    brain = _fixed()
    assert brain.dirty is False
    brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT, free_mode=True)
    assert brain.dirty is True


def test_the_control_arm_feeds_the_breakers_baseline() -> None:
    """The breaker compares what the brain chose against the factory recipe, so
    a factory turn has to land on the other side of the comparison."""
    brain = _fixed()
    brain.note(model="x/qwen3-8b", recipe=recipes.FACTORY_COMPACT,
               free_mode=True, answered=True, chars=200)
    brain.note(model="x/qwen3-8b", recipe=pool(free_mode=True)[1], free_mode=True, broken=True)

    assert brain.breaker.baseline["qwen3"], "the factory turn"
    assert brain.breaker.families["qwen3"], "and the chosen one, kept apart"


# -- what a stress run before deployment turned up -------------------------
def test_free_mode_never_explores_the_layered_prompt() -> None:
    """A free model is a small model. The layered prompt is 4,600 tokens over 52
    rules, and watching a 35B model drop most of them is what started all of
    this - so exploring it would spend one turn in ten reproducing the bug."""
    names = [r.name for r in pool(free_mode=True)]

    assert "full" not in names, names
    assert all(len(r.render()) < 6000 for r in pool(free_mode=True))


def test_paid_mode_still_has_the_whole_ladder() -> None:
    names = [r.name for r in pool(free_mode=False)]
    assert {"tight", "compact", "full"} <= set(names), names


def test_the_control_arm_does_not_starve() -> None:
    """Found by stressing it before deployment: once the bandit learns the
    factory recipe is poor it stops choosing it, so the breaker's baseline stops
    filling and the breaker can no longer judge anything at all. Half the
    exploration is now reserved for the control.

    The bar is measured rather than guessed. Over twelve seeds the control gets
    26-39 turns in 400 with the reserve and 8-23 without it, so 25 separates
    them without being tight enough to flake."""
    factory = recipes.factory_for(free_mode=True)
    worst = 400
    for seed in range(12):
        brain = Brain(on=True, rng=random.Random(seed))
        for _ in range(ENOUGH * 2):
            brain.note(model="c/command-r7b", recipe=factory, free_mode=True, broken=True)
        picks = [brain.choose(model="c/command-r7b", free_mode=True).name for _ in range(400)]
        worst = min(worst, picks.count(factory.name))

    assert worst >= 25, f"the control got as little as {worst} of 400"


def test_a_family_name_is_short_and_looks_like_a_name() -> None:
    """Ids are discovered from thirteen services, so they are not trusted to be
    short or to be words. They end up as dict keys and as lines on a screen."""
    from astolfo.brain import MAX_FAMILY

    assert family("../../etc/passwd") == "etc-passwd"
    assert len(family("a" * 400)) <= MAX_FAMILY
    assert family("🙂/🙂") == "unknown"
    for junk in ("", "/", ":free", "   "):
        assert family(junk) == "unknown", junk


def test_a_family_name_is_still_the_family() -> None:
    """Tidying must not merge two models that are genuinely different."""
    assert family("cohere/command-r-08-2024") == "command-r"
    assert family("command-r7b-12-2024") == "command-r7b"
    assert family("google/gemini-2.5-flash") == "gemini-flash"
