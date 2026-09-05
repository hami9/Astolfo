"""Three things a real group conversation showed going wrong.

Every case here is copied from the chat log: it answered Persian in English and
kept doing it after being asked twice to stop, it opened six replies in a row
with the same three words, and it appended its own English translation to a
Persian reply. A member said out loud that its whimsy had gone.
"""

from __future__ import annotations

import logging

from astolfo import chat as chat_mod
from astolfo.text import drop_translation, is_persian, looks_broken, reuses_opening
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update


async def run(rt, message, bot=None):
    await chat_mod.handle_message(make_update(message), FakeContext(rt, bot or FakeBot()))


# -- language -------------------------------------------------------------
def test_a_persian_question_answered_in_english_is_broken() -> None:
    """Verbatim: "چطوری؟" got "I'm good, thanks!", twice, then again after
    "فارسی بگو" and again after "فقط فارسی بگو"."""
    assert looks_broken("I'm good, thanks!", asked="چطوری؟")
    assert looks_broken("I'm not sure, I just do!", asked="چرا اینگلیسی مینویسی تهشو")


def test_a_persian_question_answered_in_persian_is_fine() -> None:
    assert not looks_broken("خوبم ممنون، تو چطوری؟", asked="چطوری؟")


def test_an_english_question_answered_in_english_is_fine() -> None:
    """The rule is mirror them, not always Persian."""
    assert not looks_broken("I'm good, just chilling!", asked="How are ya")


def test_finglish_is_not_mistaken_for_english() -> None:
    """Latin script, Persian language. Answering it in Latin script is correct."""
    assert not looks_broken("khoobam to chetori", asked="chetori dadash")


def test_what_counts_as_persian() -> None:
    assert is_persian("چطوری؟") and not is_persian("chetori") and not is_persian("hello")


# -- the translation it added by itself -----------------------------------
def test_an_english_gloss_is_dropped_from_a_persian_reply() -> None:
    """Verbatim: "همیشه خوبم! (I'm always good!)" - subtitles nobody asked for."""
    assert drop_translation("همیشه خوبم! (I'm always good!)") == "همیشه خوبم!"
    assert drop_translation("خوبم [I am fine]") == "خوبم"


def test_a_real_aside_in_brackets_is_left_alone() -> None:
    assert drop_translation("آره (بعله) خوبم") == "آره (بعله) خوبم"
    assert drop_translation("رفتم خونه (خیلی خسته بودم)") == "رفتم خونه (خیلی خسته بودم)"


def test_an_english_reply_is_not_stripped() -> None:
    """Only a Persian reply can be carrying a Persian-to-English gloss."""
    said = "sure thing (whenever you want)"
    assert drop_translation(said) == said


def test_the_gloss_counts_as_a_repair(rt, llm):
    """So the model that needed it is recorded as having needed it."""
    llm.reply = "همیشه خوبم! (I'm always good!)"
    shaped = chat_mod._shape(llm.reply, [])

    assert shaped.text == "همیشه خوبم!"
    assert shaped.repaired


# -- saying the same thing six times --------------------------------------
SAME_SHAPE = [
    "I'm not sure, I just do!",
    "I'm not sure I need fixing!",
    "I'm not sure what you did!",
    "I'm not sure what you mean by that!",
    "I'm not sure what you're talking about!",
]


def test_every_pair_from_the_real_run_is_caught() -> None:
    for before, after in zip(SAME_SHAPE, SAME_SHAPE[1:], strict=False):
        assert reuses_opening(after, before), after
        assert looks_broken(after, previous=before) == "opened exactly as its last reply did"


def test_two_different_replies_are_left_alone() -> None:
    assert not reuses_opening("خوبم ممنون", "آره بابا حتما")
    assert not looks_broken("waaait you're taking me right??", previous="ehehe probably!")


def test_a_short_reply_is_not_judged_on_its_opening() -> None:
    """"آره" twice is somebody agreeing twice, not a broken model."""
    assert not reuses_opening("آره", "آره")
    assert not reuses_opening("yeah!", "yeah!")


def test_an_exact_repeat_is_still_caught_by_its_own_rule() -> None:
    """The rule widened to look further back than one reply, so the wording did
    too; what it catches has only grown."""
    assert looks_broken("same thing again", previous="same thing again") == (
        "repeated something it already said"
    )


# -- the log line ---------------------------------------------------------
async def test_the_log_names_the_model_that_answered(rt, llm, caplog):
    """It used to name the model we meant to ask for. With three services out of
    allowance the answer came from the fourth, and every line said the first."""
    with caplog.at_level(logging.INFO, logger="astolfo.chat"):
        await run(rt, FakeMessage("astolfo hello"))

    assert "openrouter/" in caplog.text, "the service that answered is named"


async def test_a_detour_is_visible_in_the_log(rt, llm, caplog):
    """Three services out of allowance and the fourth answers: the line has to
    show both, or the log is quietly lying about which model wrote the reply."""
    from astolfo.llm import ChatResult, Usage

    async def answered_elsewhere(messages, **kwargs):
        llm.calls.append({"messages": messages, **kwargs})
        return ChatResult(
            text="hi", model="cohere/command-r", service="cohere", usage=Usage()
        )

    llm.chat = answered_elsewhere
    rt.settings = rt.settings.replace(model_fast="openrouter/what-we-wanted")

    with caplog.at_level(logging.INFO, logger="astolfo.chat"):
        await run(rt, FakeMessage("astolfo hello"))

    assert "cohere/command-r" in caplog.text, "the model that answered"
    assert "asked openrouter/what-we-wanted" in caplog.text, "and the one we meant to ask"


async def test_a_failed_turn_does_not_claim_a_model_answered(rt, llm, caplog):
    llm.reply = None
    with caplog.at_level(logging.INFO, logger="astolfo.chat"):
        await run(rt, FakeMessage("astolfo hello"))

    assert "| openrouter/" not in caplog.text, "nothing answered, so nothing is named"
