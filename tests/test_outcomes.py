"""What actually happened, per model and per prompt.

The bot has always known which reply it had to repair and which one leaked the
prompt; it logged both and remembered neither. These are the counters that keep
that evidence, and the rules that stop them from growing without end.
"""

from __future__ import annotations

from astolfo import chat as chat_mod
from astolfo.db import MAX_OUTCOME_ROWS, SCHEMA_VERSION, open_database, today
from astolfo.llm import Usage
from astolfo.tuning import Credit
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update


def rows(db, **where) -> list[dict]:
    found = [dict(row) for row in db.outcomes()]
    return [row for row in found if all(row[k] == v for k, v in where.items())]


# -- the counters themselves ----------------------------------------------
def test_the_same_model_and_prompt_fold_into_one_row(settings):
    db = open_database(settings.data_dir)
    for _ in range(3):
        db.add_outcome("2026-01-01", service="groq", model="qwen3-32b",
                       variant="compact", mode="fast", calls=1, completion_tokens=40)

    assert len(db.outcomes()) == 1
    row = db.outcomes()[0]
    assert row["calls"] == 3 and row["completion_tokens"] == 120


def test_the_same_model_under_a_different_prompt_is_a_different_row(settings):
    db = open_database(settings.data_dir)
    db.add_outcome("2026-01-01", service="groq", model="q", variant="compact", calls=1)
    db.add_outcome("2026-01-01", service="groq", model="q", variant="layered", calls=1)

    assert {row["variant"] for row in db.outcomes()} == {"compact", "layered"}


def test_latency_is_a_running_mean_so_one_slow_call_does_not_define_a_model(settings):
    db = open_database(settings.data_dir)
    for latency in (100, 200, 300):
        db.add_outcome("2026-01-01", service="groq", model="q", calls=1, latency_ms=latency)

    assert db.outcomes()[0]["latency_ms"] == 200


def test_an_answer_arriving_later_needs_no_call_of_its_own(settings):
    """Whether anybody replied is only known a turn after the call was counted."""
    db = open_database(settings.data_dir)
    db.add_outcome("2026-01-01", service="groq", model="q", calls=1)
    db.add_outcome("2026-01-01", service="groq", model="q", answered=1)

    row = db.outcomes()[0]
    assert row["calls"] == 1 and row["answered"] == 1


def test_a_day_cannot_write_the_file_full(settings):
    """Free mode changes model every turn, so the key space has to have a floor."""
    db = open_database(settings.data_dir)
    for n in range(MAX_OUTCOME_ROWS + 50):
        db.add_outcome("2026-01-01", service="groq", model=f"model-{n}", calls=1)

    assert len(db.outcomes(limit=10_000)) == MAX_OUTCOME_ROWS


def test_the_ceiling_is_per_day_not_forever(settings):
    db = open_database(settings.data_dir)
    for n in range(MAX_OUTCOME_ROWS):
        db.add_outcome("2026-01-01", service="groq", model=f"model-{n}", calls=1)
    db.add_outcome("2026-01-02", service="groq", model="fresh", calls=1)

    assert rows(db, day="2026-01-02", model="fresh")


def test_old_outcomes_are_pruned_with_everything_else(settings):
    db = open_database(settings.data_dir)
    db.add_outcome("2020-01-01", service="groq", model="q", calls=1)
    db.add_outcome(today(), service="groq", model="q", calls=1)

    assert db.prune(90)["outcomes"] == 1
    assert [row["day"] for row in db.outcomes()] == [today()]


def test_an_older_database_gains_the_outcomes_table(settings):
    """A running install upgrades in place; it does not start over."""
    db = open_database(settings.data_dir)
    db.execute("DROP TABLE outcomes")
    db.execute("PRAGMA user_version=6")
    db.seen_chat(-100, kind="supergroup", title="Test Group")
    db.close()

    upgraded = open_database(settings.data_dir)
    assert upgraded.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    assert upgraded.chat(-100)["title"] == "Test Group"
    upgraded.add_outcome(today(), service="groq", model="q", calls=1)
    assert len(upgraded.outcomes()) == 1


# -- what the runtime files, and what it refuses to ------------------------
def test_a_call_is_filed_under_the_service_that_served_it(rt):
    rt.record(mode="fast", model="qwen3-32b", usage=Usage(prompt_tokens=100,
              completion_tokens=20), service="groq", variant="compact",
              latency_ms=250, repaired=True, broken="leaked the prompt")

    row = rt.db.outcomes()[0]
    assert row["service"] == "groq" and row["model"] == "qwen3-32b"
    assert row["variant"] == "compact" and row["mode"] == "fast"
    assert row["calls"] == 1 and row["repaired"] == 1 and row["broken"] == 1
    assert row["prompt_tokens"] == 100 and row["latency_ms"] == 250


def test_a_call_nobody_served_is_not_filed_against_anybody(rt):
    """The router names no service, and neither does a turn every provider failed."""
    rt.record(mode="router", model="gemini-flash", usage=Usage(prompt_tokens=40))

    assert rt.db.outcomes() == []
    assert rt.budget.today_cost() == 0.0  # no cost reported, but the call is counted
    assert rt.budget.today()["calls"] == 1


def test_an_answer_is_credited_to_what_earned_it(rt):
    rt.credit_answer(Credit(service="groq", model="q", variant="compact", mode="fast"))

    assert rt.db.outcomes()[0]["answered"] == 1


def test_nothing_is_credited_when_no_model_was_involved(rt):
    """A cached reply and an offline one earn nothing, because nothing ran."""
    rt.credit_answer(Credit())

    assert rt.db.outcomes() == []


# -- end to end, through the message pipeline ------------------------------
async def run(rt, message, bot=None):
    await chat_mod.handle_message(make_update(message), FakeContext(rt, bot or FakeBot()))


async def test_a_reply_records_the_model_the_prompt_and_how_long_it_took(rt, llm):
    await run(rt, FakeMessage("astolfo hello"))

    row = rt.db.outcomes()[0]
    assert row["service"] == "openrouter"
    assert row["variant"] == chat_mod.LAYERED
    assert row["calls"] == 1 and row["latency_ms"] == 42


async def test_free_mode_is_recorded_as_the_compact_prompt(rt, llm):
    rt.settings = rt.settings.replace(free_mode=True)
    await run(rt, FakeMessage("astolfo hello"))

    assert rt.db.outcomes()[0]["variant"] == chat_mod.COMPACT


async def test_a_reply_that_had_to_be_repaired_says_so(rt, llm):
    llm.reply = "Reza: yahoo~ what's up?"
    await run(rt, FakeMessage("astolfo hello"))

    assert rt.db.outcomes()[0]["repaired"] == 1


async def test_a_clean_reply_is_not_marked_repaired(rt, llm):
    await run(rt, FakeMessage("astolfo hello"))

    row = rt.db.outcomes()[0]
    assert row["repaired"] == 0 and row["broken"] == 0


async def test_answering_the_bot_credits_the_model_that_spoke_not_the_next_one(rt, llm):
    """Free mode changes model between turns; the credit must not follow it."""
    await run(rt, FakeMessage("astolfo hello"))
    llm.reply = "still here~"
    rt.settings = rt.settings.replace(model_fast="a-different-model")
    await run(rt, FakeMessage("astolfo still there?"))

    credited = [row for row in rt.db.outcomes() if row["answered"]]
    assert len(credited) == 1
    assert credited[0]["model"] != "a-different-model"


async def test_walking_away_credits_nobody(rt, llm):
    rt.settings = rt.settings.replace(group_reply_chance=0.0)
    await run(rt, FakeMessage("astolfo hello"))
    await run(rt, FakeMessage("anyway, about lunch"))

    assert not [row for row in rt.db.outcomes() if row["answered"]]


async def test_a_reply_served_from_the_cache_records_nothing(rt, llm):
    rt.settings = rt.settings.replace(response_cache=True)
    await run(rt, FakeMessage("astolfo hello"))
    before = len(rt.db.outcomes())
    await run(rt, FakeMessage("astolfo hello"))

    assert len(llm.calls) == 1, "the second turn was served from the cache"
    assert len(rt.db.outcomes()) == before
