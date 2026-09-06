"""A reply that gets stuck repeating itself inside one message.

Every other repetition check compares a reply against earlier replies, so a
model looping within a single message passed all of them and reached the group.
This is the transcript that found it.
"""

from __future__ import annotations

from astolfo.text import LOOP_REPEATS, looks_broken, loops_internally

# Verbatim from the group, cut off by the token ceiling exactly as it was there.
FROM_THE_GROUP = (
    "هه، ببین، من اصلاً نمی‌دونم کدوم‌شون بهتره، ولی ددی کاپیتانو خیلی خفن بود، آره؟ "
    "خیلی خفن! ولی ماوویکا مید هم خیلی قوی بود، آره؟ خیلی قوی! ولی ددی کاپیتانو خیلی "
    "خفن بود، آره؟ خیلی خفن! ولی ماوویکا مید هم خیلی قوی بود، آره؟ خیلی قوی! ولی ددی "
    "کاپیتانو خیلی خفن بود، آره؟ خیلی خفن! ولی ماوویکا مید هم خیلی قوی بود، آره؟ خیلی "
    "قوی! ولی ددی کاپیتانو خیلی خفن بود، آره؟ خیلی"
)


def test_the_reply_that_reached_the_group_is_caught():
    assert looks_broken(FROM_THE_GROUP, asked="ددی کاپیتانو یا ماوویکا مید؟") is not None


def test_it_is_caught_as_a_loop_and_not_by_accident():
    assert loops_internally(FROM_THE_GROUP)


def test_english_loops_too():
    reply = "I think so, yes I do. I think so, yes I do. I think so, yes I do."
    assert loops_internally(reply)


def test_a_phrase_said_twice_is_emphasis_not_a_loop():
    reply = "خیلی خفن بود، آره؟ خیلی خفن بود، آره؟ ولی خب، بستگی داره به تیمت."
    assert LOOP_REPEATS == 3
    assert not loops_internally(reply)


def test_an_ordinary_reply_survives():
    for reply in (
        "نمی‌دونم والا، ولی اگه مجبورم کنی می‌گم مافویکا. تیمت چیه؟",
        "Two.",
        "hahaha okay okay, you win this one",
        "چای یا قهوه؟ من چای، تو چی؟",
    ):
        assert not loops_internally(reply), reply
        assert looks_broken(reply) is None, reply


def test_a_long_reply_saying_almost_nothing_is_a_loop():
    """No punctuation to split on, so the give-away is the vocabulary."""
    reply = " ".join(["خیلی قوی آره"] * 9)
    assert loops_internally(reply)


def test_a_long_varied_reply_is_not(persian_essay="""
    راستش بستگی داره چی می‌خوای ازش. مافویکا برای دیمیج پایداره و با تیم آتش
    خوب جفت می‌شه، ولی کاپیتانو بیشتر به دردت می‌خوره اگه تیمت شیلد کم داره.
    من خودم دومی رو برداشتم چون آرتیفکتاش رو از قبل داشتم و نمی‌خواستم دوباره
    فارم کنم. تو رزینت رو کجا خرج می‌کنی این روزا؟
"""):
    assert not loops_internally(persian_essay)
