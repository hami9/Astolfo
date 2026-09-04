from collections import deque
from types import SimpleNamespace

from astolfo.memory import ChatState
from astolfo.tuning import ENOUGH, LONG, SHORT, Reception, brevity_hint, reply_ceiling


def _state() -> ChatState:
    return ChatState(chat_id=1, history=deque(maxlen=10))


def _rt(settings, spent: float = 0.0):
    return SimpleNamespace(
        settings=settings, budget=SimpleNamespace(today_cost=lambda: spent)
    )


def test_a_bucket_is_chosen_by_length():
    reception = Reception()
    reception.note_sent(50)
    reception.note_sent(600)
    assert reception.sent[SHORT] == 1
    assert reception.sent[LONG] == 1


def test_an_answer_is_credited_to_the_reply_it_answered():
    reception = Reception()
    reception.note_sent(50)
    reception.note_answered()
    assert reception.rate(SHORT) == 1.0

    reception.note_sent(600)
    reception.note_ignored()
    assert reception.rate(LONG) == 0.0


def test_one_answer_is_counted_once():
    reception = Reception()
    reception.note_sent(50)
    reception.note_answered()
    reception.note_answered()
    assert reception.answered[SHORT] == 1


def test_nothing_is_concluded_from_too_little():
    reception = Reception()
    for _ in range(ENOUGH - 1):
        reception.note_sent(50)
        reception.note_answered()
    assert reception.best() == "", "one bucket with a few samples proves nothing"


def test_a_clear_winner_is_picked():
    reception = Reception()
    for _ in range(ENOUGH):
        reception.note_sent(50)
        reception.note_answered()
        reception.note_sent(600)
        reception.note_ignored()
    assert reception.best() == SHORT


def test_a_near_tie_changes_nothing():
    reception = Reception()
    for index in range(ENOUGH):
        reception.note_sent(50)
        reception.note_answered()
        reception.note_sent(600)
        if index:  # every long one answered but the first is not a real difference
            reception.note_answered()
        else:
            reception.note_ignored()
    assert reception.best() == ""


def test_the_configured_ceiling_stands_until_the_chat_says_otherwise(settings):
    state = _state()
    assert reply_ceiling(_rt(settings), state, base=160) == 160
    assert brevity_hint(state) == ""


def test_a_chat_that_ignores_long_replies_gets_shorter_ones(settings):
    state = _state()
    for _ in range(ENOUGH):
        state.reception.note_sent(50)
        state.reception.note_answered()
        state.reception.note_sent(600)
        state.reception.note_ignored()

    assert reply_ceiling(_rt(settings), state, base=160) < 160
    assert "One line" in brevity_hint(state)


def test_a_think_answer_keeps_its_room(settings):
    state = _state()
    for _ in range(ENOUGH):
        state.reception.note_sent(50)
        state.reception.note_answered()
        state.reception.note_sent(600)
        state.reception.note_ignored()
    assert reply_ceiling(_rt(settings), state, base=900, mode_is_fast=False) == 900


def test_a_tight_budget_shortens_everything(settings):
    tight = settings.replace(daily_budget_usd=1.0)
    state = _state()
    full = reply_ceiling(_rt(tight, spent=0.0), state, base=160)
    half = reply_ceiling(_rt(tight, spent=0.8), state, base=160)
    spent = reply_ceiling(_rt(tight, spent=1.0), state, base=160)
    assert spent < half < full


def test_the_ceiling_never_falls_to_nothing(settings):
    tight = settings.replace(daily_budget_usd=1.0)
    assert reply_ceiling(_rt(tight, spent=99.0), _state(), base=160) >= 60


def test_counters_survive_a_round_trip():
    reception = Reception()
    reception.note_sent(50)
    reception.note_answered()
    again = Reception.load(reception.as_dict())
    assert again.rate(SHORT) == 1.0
    assert Reception().as_dict() == {}


def test_a_broken_stored_value_starts_the_counters_over():
    assert Reception.load("not a dict").as_dict() == {}
    assert Reception.load({"sent": "nonsense"}).as_dict() == {}
    assert Reception.load({"sent": {SHORT: "many"}}).as_dict() == {}
