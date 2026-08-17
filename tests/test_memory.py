from collections import deque

from astolfo.memory import ChatState, ChatStore, update_notes


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
    contents = [turn["content"] for turn in selected]
    assert 1 <= len(selected) <= 3
    assert sum(len(c) for c in contents) <= 300
    assert contents == sorted(contents, key=lambda c: int(c.split("msg")[1].split()[0]))
    assert "msg8" in contents[-1], "the newest turns must be the ones kept"


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
    store = ChatStore(settings)
    state = store.get(101)
    state.notes = "Reza loves coffee"
    state.reply_chance = 0.5
    state.forced_mode = "think"
    state.locale = "fa"
    state.add_user("reza", "secret message")
    store.mark_dirty()
    store.save()

    reloaded = ChatStore(settings).get(101)
    assert reloaded.notes == "Reza loves coffee"
    assert reloaded.reply_chance == 0.5
    assert reloaded.forced_mode == "think"
    assert reloaded.locale == "fa"
    assert len(reloaded.history) == 0


def test_store_lru_eviction(settings):
    store = ChatStore(settings.replace(max_chats=3))
    for chat_id in range(6):
        store.get(chat_id)
    assert len(store.all_states()) <= 3


async def test_update_notes_merges(settings, llm):
    active = settings.replace(summaries=True)
    state = ChatState(chat_id=1, history=deque(maxlen=4))
    for i in range(4):
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
