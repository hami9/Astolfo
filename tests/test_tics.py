"""One evening where the bot said the same thing fifteen times.

Every line here is verbatim from the group. The guard that exists for exactly
this - `looks_broken` - passed all of them, because it compared three words
against the single reply before, and the tic was one word repeating across a
dozen turns.
"""

from __future__ import annotations

from astolfo.text import SAME_FIRST_WORD, looks_broken, opens_like_recent, repeats_recent

# In order, as they were sent.
EVENING = [
    "اوه، من از فحش دادن خوشم نمیاد!",
    "ارمان؟ اوه، خیلی باحاله!",
    "اوه، پس چرا فحش داد؟!",
    "اوه، حتماً یه دلیل داشته!",
    "هی هی، منو ببخش!",
    "اوه، من از زامبیا می‌ترسم!",
    "اوه، انگشت کوچولو؟!",
    "اوه، منم سوال دارم!",
    "اوه، منم همینطور!",
    "اوه، من متا بازی بلد نیستم!",
    "اوه، من خنگ نیستم!",
    "اوه، من دلقک نیستم!",
]


def _said(upto: int) -> list[str]:
    """What the bot had already said by then, newest first."""
    return list(reversed(EVENING[:upto]))


# -- the tic ---------------------------------------------------------------
def test_the_opening_word_repeating_is_caught() -> None:
    """No two of these share three words, which is why nothing ever fired."""
    assert opens_like_recent(EVENING[11], _said(11))


def test_it_is_caught_the_moment_it_is_a_habit_and_not_before() -> None:
    """A word used twice is a word. Used three times in six replies it is a tic."""
    recent = ["اوه، اولی", "چیز دیگه", "اوه، دومی", "اوه، سومی"]
    assert not opens_like_recent("اوه، چهارمی", recent[:3]), "twice is a word"
    assert opens_like_recent("اوه، چهارمی", recent), "three times is a habit"
    assert SAME_FIRST_WORD == 3


def test_the_whole_reply_is_judged_not_only_the_last_turn() -> None:
    """It fires through looks_broken, which is what the retry actually reads."""
    said = _said(11)
    assert looks_broken(EVENING[11], previous=said[0], recent=said[1:]) == (
        "opened the way it keeps opening"
    )


def test_a_chat_that_simply_talks_normally_is_left_alone() -> None:
    """A guard that eats real replies is its own failure."""
    ordinary = [
        "آره بابا، همون بازیه",
        "نمی‌دونم راستش، بذار ببینم",
        "هه‌هه چه باحال",
        "من که پایه‌ام",
    ]
    assert not opens_like_recent("خب پس فردا می‌بینمت", ordinary)
    assert not looks_broken("خب پس فردا می‌بینمت", previous=ordinary[0], recent=ordinary[1:])


def test_english_tics_are_caught_the_same_way() -> None:
    """Six replies in a row began "I'm not sure" once, in English."""
    said = ["I'm not sure about that", "I'm sorry, what?", "I'm not really into it"]
    assert opens_like_recent("I'm here!", said)


# -- the sentence that kept coming back ------------------------------------
def test_a_sentence_from_further_back_than_one_reply_is_caught() -> None:
    """"من یه سروکار دارم با تو" came back three times, never twice in a row, so a
    check holding one previous reply could not see it."""
    line = "من؟ من یه سروکار دارم با تو!"
    said = ["اوه، من دلقک نیستم!", "من خنگ نیستم!", line, "چیز دیگه"]

    assert repeats_recent(line, said)
    assert looks_broken(line, previous=said[0], recent=said[1:]) == (
        "repeated something it already said"
    )


def test_punctuation_and_spacing_do_not_hide_a_repeat() -> None:
    assert repeats_recent("Same   Thing  Again", ["same thing again"])


def test_a_reply_nobody_said_before_is_not_a_repeat() -> None:
    assert not repeats_recent("یه چیز کاملاً جدید", EVENING)


def test_nothing_said_yet_is_never_a_repeat() -> None:
    assert not repeats_recent("hello", [])
    assert not opens_like_recent("hello", [])
    assert not looks_broken("hello there, what's up~")


# -- the one that mattered most --------------------------------------------
def test_somebody_asking_how_to_get_over_a_girl_is_not_banter() -> None:
    """Verbatim. He asked, then said he had loved her and the longer it went the
    clearer it was she did not care - and got "oh, I have a question too!" back.
    None of the crisis words are in that, and none of them should have to be."""
    from astolfo.persona import SERIOUS
    from astolfo.routing import heuristic

    for said in (
        "چطور از یه دختری که محل‌سگ بهم نمیده move on کنم؟",
        "من خیلی دوسش داشتم ولی هرچی بیشتر طول کشید بیشتر فهمیدم که اون اصلا بهم اهمیتی نمیده",
    ):
        decision, confidence = heuristic(said)
        assert decision.mode == SERIOUS, said
        assert confidence >= 0.85, "confident enough not to ask a free router model"


def test_the_crisis_tier_is_still_its_own_thing() -> None:
    """Heartbreak is not an emergency, and the reasons stay apart in the log."""
    from astolfo.routing import heuristic

    assert heuristic("خودکشی کنم بهتره")[0].reason == "distress signals"
    assert heuristic("دلم شکست")[0].reason == "somebody is hurting"


def test_it_is_about_the_person_talking_not_about_gossip() -> None:
    """Somebody else's breakup is a story, not a confidence."""
    from astolfo.persona import SERIOUS
    from astolfo.routing import heuristic

    for ordinary in (
        "ارمان دوسش داشت اون بازیو",
        "دوسش دارم اون آهنگو",
        "lets move on to the next map",
        "ok lets move on",
        "بریم بازی؟",
        "این گوشیه به درد نمی‌خوره",
        "میابی تو متا بازی چقدر خوبه",
    ):
        assert heuristic(ordinary)[0].mode != SERIOUS, ordinary


def test_a_half_space_does_not_hide_it() -> None:
    """The real message wrote محل‌سگ with a zero-width non-joiner in it."""
    from astolfo.persona import SERIOUS
    from astolfo.routing import heuristic

    assert heuristic("دل‌شکسته‌ام")[0].mode == SERIOUS
    assert heuristic("دل شکسته ام")[0].mode == SERIOUS


def test_english_move_on_still_has_to_say_who() -> None:
    """In Persian it is the borrowed phrase for getting over somebody. In English
    it is as likely to be about the next map."""
    from astolfo.persona import SERIOUS
    from astolfo.routing import heuristic

    assert heuristic("i need to move on from her")[0].mode == SERIOUS
    assert heuristic("alright, move on")[0].mode != SERIOUS
