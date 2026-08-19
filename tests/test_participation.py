"""How talkative the bot is, and the caps that stop one group eating the day."""

from __future__ import annotations

from astolfo import participation
from astolfo.chat import handle_message
from astolfo.db import open_database
from tests.conftest import FakeContext, FakeMessage, make_update


def _busy(state, messages: int = 40) -> None:
    """Fill the arrival times so the chat looks like it is moving fast."""
    now = state.seen_at[-1] if state.seen_at else 0.0
    for index in range(messages):
        state.seen_at.append(now + index * 0.5)  # two a second


# -- picking the mode -----------------------------------------------------
def test_manual_never_volunteers(rt):
    state = rt.store.get(-100)
    state.mode = participation.MANUAL
    assert participation.should_join(rt, state, "") is False


def test_auto_joins_when_the_dice_say_so(rt):
    rt.settings = rt.settings.replace(group_reply_chance=1.0)
    state = rt.store.get(-100)
    state.mode = participation.AUTO
    assert participation.should_join(rt, state, "") is True


def test_a_group_setting_beats_the_global_one(rt):
    rt.settings = rt.settings.replace(reply_mode=participation.MANUAL, group_reply_chance=1.0)
    state = rt.store.get(-100)
    state.mode = participation.AUTO
    assert participation.should_join(rt, state, "") is True


def test_smart_joins_a_quiet_chat(rt):
    rt.settings = rt.settings.replace(reply_mode=participation.SMART, group_reply_chance=1.0)
    state = rt.store.get(-100)
    mode, why = participation.effective(rt, state)
    assert mode == participation.AUTO, why


def test_smart_goes_quiet_when_the_chat_is_busy(rt):
    rt.settings = rt.settings.replace(reply_mode=participation.SMART, group_reply_chance=1.0)
    state = rt.store.get(-100)
    _busy(state)

    mode, why = participation.effective(rt, state)
    assert mode == participation.MANUAL
    assert "busy" in why
    assert participation.should_join(rt, state, "") is False


def test_smart_goes_quiet_when_nothing_can_answer(rt, llm):
    rt.settings = rt.settings.replace(reply_mode=participation.SMART, group_reply_chance=1.0)
    llm.reachable = False
    mode, why = participation.effective(rt, rt.store.get(-100))
    assert (mode, "resting" in why) == (participation.MANUAL, True)


def test_smart_goes_quiet_near_the_budget(rt):
    rt.settings = rt.settings.replace(
        reply_mode=participation.SMART, group_reply_chance=1.0, daily_budget_usd=1.0
    )
    rt.budget.record(mode="fast", model="m", usage=_spend(0.8))

    mode, why = participation.effective(rt, rt.store.get(-100))
    assert mode == participation.MANUAL
    assert "budget" in why


def _spend(cost: float):
    from astolfo.llm import Usage

    return Usage(prompt_tokens=1, completion_tokens=1, cost=cost)


async def test_being_addressed_ignores_the_mode(rt, bot):
    """Manual means "only when spoken to", not "silent"."""
    rt.settings = rt.settings.replace(reply_mode=participation.MANUAL)
    message = FakeMessage("astolfo are you there?")

    await handle_message(make_update(message), FakeContext(rt, bot))
    assert message.sent, "an addressed message is always answered"


# -- limits ---------------------------------------------------------------
async def test_a_group_limit_stops_that_group_only(rt, llm, bot):
    state = rt.store.get(-100)
    state.daily_limit = 1

    first = FakeMessage("astolfo one")
    await handle_message(make_update(first), FakeContext(rt, bot))
    assert first.sent, "the first one is within the limit"

    second = FakeMessage("astolfo two")
    await handle_message(make_update(second), FakeContext(rt, bot))
    assert len(llm.calls) == 1, "the second is over the limit"

    elsewhere = FakeMessage("astolfo three", chat_id=-200)
    await handle_message(make_update(elsewhere), FakeContext(rt, bot))
    assert len(llm.calls) == 2, "another group is unaffected"


async def test_a_person_limit_stops_that_person_only(rt, llm, bot):
    rt.set_user_limit(7, 1)

    for text in ("astolfo one", "astolfo two"):
        await handle_message(make_update(FakeMessage(text, user_id=7)), FakeContext(rt, bot))
    assert len(llm.calls) == 1, "their second message is over their limit"

    await handle_message(
        make_update(FakeMessage("astolfo hello", user_id=8)), FakeContext(rt, bot)
    )
    assert len(llm.calls) == 2, "somebody else still gets an answer"


def test_a_person_limit_survives_a_restart(settings, rt):
    from astolfo.runtime import Runtime

    rt.set_user_limit(9, 25)
    assert Runtime.build(settings).limit_for(9) == 25

    rt.set_user_limit(9, 0)
    assert Runtime.build(settings).limit_for(9) == 0


def test_every_group_can_be_set_at_once(settings):
    db = open_database(settings.data_dir)
    for chat_id in (-100, -200, -300):
        db.joined_chat(chat_id, kind="supergroup", title=f"Group {chat_id}")

    assert db.set_every_chat(mode=participation.MANUAL, daily_limit=50) == 3
    assert all(row["mode"] == participation.MANUAL for row in db.list_chats())
    assert all(row["daily_limit"] == 50 for row in db.list_chats())


def test_a_group_the_bot_has_left_is_not_touched(settings):
    db = open_database(settings.data_dir)
    db.joined_chat(-100, kind="supergroup", title="Here")
    db.joined_chat(-200, kind="supergroup", title="Gone")
    db.left_chat(-200)

    assert db.set_every_chat(daily_limit=5) == 1
