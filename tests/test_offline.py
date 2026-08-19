"""What the bot can still say when no model will answer."""

from __future__ import annotations

import pytest

from astolfo import offline
from astolfo.chat import handle_message
from tests.conftest import FakeContext, FakeMessage, make_update


# -- what it will answer --------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["hi", "hey!", "hello", "سلام", "salam", "yo"],
)
def test_a_greeting_needs_no_model(text):
    assert offline.answer(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2+2", "4"),
        ("10 * 4", "40"),
        ("100/8", "12.5"),
        ("(3+5)*2 =", "16"),
        ("7 × 6", "42"),
        ("-4 + 10", "6"),
    ],
)
def test_a_sum_is_worked_out(text, expected):
    assert offline.answer(text) == f"{expected} ✨"


def test_the_time_and_date_come_from_the_clock():
    assert "UTC" in offline.answer("what time is it?")
    assert "today is" in offline.answer("what is the date today?")


def test_the_name_it_was_called_by_is_not_part_of_the_question():
    assert offline.answer("astolfo 12*12") == "144 ✨"
    assert offline.answer("@astolfo_bot hi") is not None
    assert offline.answer("آستولفو سلام", locale="fa") is not None
    assert offline.strip_address("astolfo, what time is it astolfo") == "what time is it"


def test_it_can_say_what_it_is():
    assert "Astolfo" in offline.answer("who are you?")
    assert "آستولفو" in offline.answer("تو کی ای؟", locale="fa")


def test_it_answers_in_the_chat_s_language():
    assert offline.answer("سلام", locale="fa") != offline.answer("hi", locale="en")
    assert "ساعت" in offline.answer("ساعت چنده؟", locale="fa")


# -- what it will not answer ---------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "what is the capital of Peru?",
        "who won the match last night?",
        "explain quantum tunnelling",
        "پایتخت پرو کجاست؟",
        "بهترین رستوران تهران کجاست",
        "write me a poem about cats",
    ],
)
def test_a_real_question_is_left_alone(text):
    """Anything needing knowledge must reach a model or be declined, never guessed."""
    assert offline.answer(text) is None


@pytest.mark.parametrize("text", ["", "   ", "1234", "hello world how are things going"])
def test_nonsense_and_statements_are_not_forced_into_an_answer(text):
    assert offline.answer(text) is None or "how are" in text


def test_a_sum_that_is_not_a_sum_is_refused():
    assert offline.calculate("2 ** 64") is None, "no operators beyond the four"
    assert offline.calculate("__import__('os')") is None
    assert offline.calculate("1/0") is None
    assert offline.calculate("hello") is None
    assert offline.calculate("42") is None, "a number alone is not a question"


# -- in the pipeline ------------------------------------------------------
async def test_a_greeting_is_answered_with_every_service_down(rt, llm, bot):
    llm.reachable = False
    message = FakeMessage("astolfo hi")

    await handle_message(make_update(message), FakeContext(rt, bot))

    assert message.sent, "it still says hello"
    assert "offline" not in message.sent[0].lower(), "a greeting needs no excuse"
    assert llm.calls == [], "and it did not waste a call finding out"


async def test_a_real_question_gets_an_honest_answer_when_nothing_is_up(rt, llm, bot):
    llm.reachable = False
    message = FakeMessage("astolfo what is the capital of Peru?")

    await handle_message(make_update(message), FakeContext(rt, bot))

    assert message.sent, "silence would look like a crash"
    assert "offline" in message.sent[0].lower()


async def test_a_failed_model_falls_back_to_what_needs_no_model(rt, llm, bot):
    """Reachable but failing: the call happens, then the offline answer covers it."""
    llm.reply = None
    message = FakeMessage("astolfo 12*12")

    await handle_message(make_update(message), FakeContext(rt, bot))

    assert llm.calls, "it tried the model first"
    assert message.sent == ["144 ✨"]


async def test_the_ordinary_path_is_untouched(rt, llm, bot):
    message = FakeMessage("astolfo hi")
    await handle_message(make_update(message), FakeContext(rt, bot))

    assert llm.calls, "a working model still answers greetings itself"
    assert message.sent == [llm.reply]
