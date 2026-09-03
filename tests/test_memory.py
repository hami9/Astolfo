from collections import deque

from astolfo.db import open_database
from astolfo.memory import SUMMARY_EVERY, ChatState, ChatStore, history_budget, update_notes


def _state(maxlen: int = 10) -> ChatState:
    return ChatState(chat_id=1, history=deque(maxlen=maxlen))


def test_history_is_bounded():
    state = _state(3)
    for i in range(10):
        state.add_user(f"user{i}", f"message {i}")
    assert len(state.history) == 3
    assert state.turn_count == 10
    assert state.recent_texts(2) == ["user8: message 8", "user9: message 9"]


def test_participants_are_capped():
    state = _state()
    for i in range(40):
        state.touch_participant(f"user{i}")
    assert len(state.participants) == 20
    assert "user39" in state.participants


def test_prompt_history_respects_char_budget():
    state = _state(20)
    for i in range(10):
        state.add_user("reza", f"msg{i} " + "x" * 100)

    selected = state.prompt_history(char_budget=300)
    body = "\n".join(turn["content"] for turn in selected)
    assert len(body) <= 320
    assert "msg8" in body, "the newest turns must be the ones kept"
    assert "msg0" not in body, "the oldest turns must be dropped"


def test_prompt_history_skips_current_turn():
    state = _state()
    state.add_user("reza", "first")
    state.add_user("reza", "second")
    contents = [turn["content"] for turn in state.prompt_history(10_000)]
    assert contents == ["reza: first"]


def test_prompt_history_keeps_at_least_one_turn():
    state = _state()
    state.add_user("reza", "y" * 5000)
    state.add_user("reza", "current")
    assert len(state.prompt_history(char_budget=10)) == 1


def test_store_persists_settings_but_not_messages(settings):
    store = ChatStore(settings, open_database(settings.data_dir))
    state = store.get(101)
    state.notes = "Reza loves coffee"
    state.reply_chance = 0.5
    state.forced_mode = "think"
    state.locale = "fa"
    state.add_user("reza", "secret message")
    store.mark_dirty()
    store.save()

    reloaded = ChatStore(settings, open_database(settings.data_dir)).get(101)
    assert reloaded.notes == "Reza loves coffee"
    assert reloaded.reply_chance == 0.5
    assert reloaded.forced_mode == "think"
    assert reloaded.locale == "fa"
    assert len(reloaded.history) == 0


def test_store_lru_eviction(settings):
    small = settings.replace(max_chats=3)
    store = ChatStore(small, open_database(small.data_dir))
    for chat_id in range(6):
        store.get(chat_id)
    assert len(store.all_states()) <= 3


async def test_update_notes_merges(settings, llm):
    active = settings.replace(summaries=True)
    state = ChatState(chat_id=1, history=deque(maxlen=40))
    for i in range(SUMMARY_EVERY):
        state.add_user("reza", f"message {i}")

    llm.json_result = {"notes": "Reza is planning a trip"}
    usage = await update_notes(llm, active, state)

    assert state.notes == "Reza is planning a trip"
    assert usage.total_tokens > 0
    assert state.summarizing is False


async def test_update_notes_skipped_when_history_is_short(settings, llm):
    state = ChatState(chat_id=1, history=deque(maxlen=10))
    state.add_user("reza", "hi")
    await update_notes(llm, settings.replace(summaries=True), state)
    assert llm.json_calls == []


async def test_update_notes_ignores_garbage(settings, llm):
    state = ChatState(chat_id=1, history=deque(maxlen=2))
    state.add_user("reza", "a")
    state.add_user("reza", "b")
    llm.json_result = None
    await update_notes(llm, settings.replace(summaries=True), state)
    assert state.notes == ""


def test_unanswered_messages_collapse_into_one_turn():
    """A run of user turns must not look like a queue of separate questions."""
    state = _state(20)
    state.add_user("Hami", "what are these?")
    state.add_user("Hami", "edit it?")
    state.add_user("DanTRM", "chance 25?")
    state.add_user("Hami", "it was 100")
    state.add_user("DanTRM", "current one")

    turns = state.prompt_history(10_000)
    assert len(turns) == 1, "consecutive user turns must be merged into one"
    assert turns[0]["role"] == "user"
    assert turns[0]["content"].splitlines() == [
        "Hami: what are these?",
        "Hami: edit it?",
        "DanTRM: chance 25?",
        "Hami: it was 100",
    ]


def test_merging_keeps_the_conversation_shape():
    state = _state(20)
    state.add_user("Hami", "one")
    state.add_user("Sara", "two")
    state.add_assistant("ehehe~")
    state.add_user("Hami", "three")
    state.add_user("Sara", "four")
    state.add_user("Hami", "current")

    roles = [turn["role"] for turn in state.prompt_history(10_000)]
    assert roles == ["user", "assistant", "user"]


def test_merge_runs_does_not_mutate_the_stored_history():
    state = _state(20)
    state.add_user("Hami", "one")
    state.add_user("Sara", "two")
    state.add_user("Hami", "current")

    state.prompt_history(10_000)
    assert [turn["content"] for turn in state.history] == [
        "Hami: one",
        "Sara: two",
        "Hami: current",
    ]


# -- how much history actually fits ---------------------------------------
def test_a_small_model_gets_less_history_than_it_was_asked_for():
    """9000 characters into an 8k model pushes the persona out of the window."""
    budget = history_budget(9000, context_tokens=8000, overhead_chars=10000, reply_tokens=260)
    assert budget < 9000
    assert budget >= 1200, "never so little that there is no conversation left"


def test_a_large_model_gets_exactly_what_was_configured():
    budget = history_budget(9000, context_tokens=1_000_000, overhead_chars=10000, reply_tokens=900)
    assert budget == 9000


def test_an_unknown_model_is_trusted_with_the_configured_budget():
    """The catalog does not name every model; the setting is the fallback."""
    assert history_budget(9000, context_tokens=0, overhead_chars=99999, reply_tokens=900) == 9000


def test_the_floor_holds_even_when_the_prompt_alone_overflows():
    budget = history_budget(9000, context_tokens=4000, overhead_chars=40000, reply_tokens=900)
    assert budget == 1200


# -- when notes are folded ------------------------------------------------
async def test_notes_form_long_before_the_window_is_full(settings, llm):
    """A chat that had said 79 things used to have no long-term memory at all."""
    active = settings.replace(summaries=True)
    state = ChatState(chat_id=1, history=deque(maxlen=80))
    for i in range(SUMMARY_EVERY):
        state.add_user("reza", f"message {i}")

    llm.json_result = {"notes": "Reza is planning a trip"}
    await update_notes(llm, active, state)

    assert state.notes == "Reza is planning a trip"
    assert len(state.history) < 80, "nowhere near full"


async def test_the_same_turns_are_not_folded_twice(settings, llm):
    active = settings.replace(summaries=True)
    state = ChatState(chat_id=1, history=deque(maxlen=80))
    llm.json_result = {"notes": "something"}

    for i in range(SUMMARY_EVERY):
        state.add_user("reza", f"message {i}")
    await update_notes(llm, active, state)
    assert len(llm.json_calls) == 1

    # Nothing new has been said, so there is nothing new to remember.
    await update_notes(llm, active, state)
    assert len(llm.json_calls) == 1

    for i in range(SUMMARY_EVERY):
        state.add_user("reza", f"later {i}")
    await update_notes(llm, active, state)
    assert len(llm.json_calls) == 2
    sent = llm.json_calls[-1]["messages"][-1]["content"]
    assert "message 0" not in sent, "already folded once"
