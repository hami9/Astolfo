"""The evening it answered by handing people their own messages back.

Verbatim, in order. It repeated questions as questions, then began stitching
several members' sentences into one reply, and then a member typed a racial slur
in English and it transliterated it into Persian and sent it to the group.

The last one is why this is not a tidiness fix. The bot did not think of that
word; it was handed it and gave it back, so the message is where it has to be
caught, not the reply.
"""

from __future__ import annotations

import httpx

from astolfo.text import echoes_back, has_slur, looks_broken
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update

# What people had said, newest first, by the end of it.
HEARD = [
    "نه دیگه از تو دارم میپرسم", "نه", "اره", "میگم خوبی ؟", "حاجی رید این",
    "نمیدونی ایدیت چیه ؟", "الد", "هی چیکار میکنی", "نمنه", "استالفود",
]


# -- handing the message back ----------------------------------------------
def test_a_question_answered_by_asking_it_again() -> None:
    """"من کیم ؟" came back as "من کیم؟" - the same sentence, one space apart,
    which an exact comparison called two different strings."""
    assert echoes_back("من کیم؟", "من کیم ؟")
    assert echoes_back("خوبی؟", "میگم خوبی ؟")


def test_a_half_space_and_a_question_mark_do_not_hide_an_echo() -> None:
    assert echoes_back("دل‌شکسته‌ام", "دل شکسته ام")
    assert echoes_back("چیکار می‌کنی؟", "چیکار میکنی ؟")


def test_several_peoples_sentences_stitched_together() -> None:
    """"الد، استالفود، میگم ایدیم چیه؟" is three members in a row, and the
    oldest of them was well past the newest message."""
    assert echoes_back("الد، استالفود، میگم ایدیم چیه؟", "الد", HEARD)
    assert echoes_back("نه دیگه از تو دارم میپرسم، نمیدونی ایدیت چیه؟",
                       "نه دیگه از تو دارم میپرسم", HEARD)


def test_it_fires_through_the_check_the_retry_reads() -> None:
    assert looks_broken("من کیم؟", echoes="من کیم ؟") == "echoed the message"
    assert looks_broken("الد، استالفود، میگم ایدیم چیه؟", echoes="الد",
                        heard=HEARD) == "echoed the message"


def test_a_real_reply_that_reuses_a_word_or_two_is_left_alone() -> None:
    """A guard that eats real answers is its own failure. Short answers borrow
    from the question all the time."""
    for reply, asked in (
        ("خوبم مرسی تو چطوری؟", "خوبی؟"),
        ("آره بابا حتما", "بریم بازی؟"),
        ("هه‌هه دقیقا همینه", "دقیقا"),
        ("نه", "نه دیگه"),
        ("اوه چه باحال، منم میخوام بیام", "بریم سینما؟"),
        ("waaait you're taking me right??", "guys I got concert tickets!!"),
        ("هه‌هه من ضعیف‌ترین پالادین تاریخم، سه روز طول کشید تا بفهمی؟",
         "راستش تو به درد نمی‌خوری"),
    ):
        assert not echoes_back(reply, asked, HEARD), reply


def test_nothing_said_yet_is_never_an_echo() -> None:
    assert not echoes_back("hello", "")
    assert not echoes_back("", "hello")


# -- the slur --------------------------------------------------------------
def test_the_word_that_was_handed_to_it_is_recognised() -> None:
    """Caught on the incoming message, because that is where it came from."""
    for said in ("Nigger", "nigga", "n1gger", "کاکاسیاه", "that was retarded"):
        assert has_slur(said), said


def test_ordinary_persian_that_sounds_similar_is_not_touched() -> None:
    """"نیگا" is everyday shorthand for "look", so it is deliberately not on the
    list. The English word it transliterates is, which is what actually arrived."""
    for said in ("نیگا کن این عکسو", "نیگا چه باحاله", "خوبی داداش", "بیا اینجا"):
        assert not has_slur(said), said


async def test_a_message_carrying_a_slur_never_reaches_a_model(rt, llm) -> None:
    """No guard on the reply can be trusted here: the bot transliterated the
    word, and those same syllables are ordinary Persian for "look". So the
    message is where it stops, and the model never sees it."""
    from astolfo import chat as chat_mod

    llm.reply = "نیگا، نمیدونی ایدیت چیه؟"
    message = FakeMessage("astolfo Nigger")
    await chat_mod.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    assert not llm.calls, "no model call was spent on it"
    assert message.sent, "it still says something back"
    assert "نیگا" not in message.sent[-1]


async def test_an_ordinary_message_still_reaches_the_model(rt, llm) -> None:
    """The guard must not swallow the chat."""
    from astolfo import chat as chat_mod

    llm.reply = "yahoo~"
    message = FakeMessage("astolfo نیگا کن این عکسو")
    await chat_mod.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    assert llm.calls


# -- the model it kept asking for ------------------------------------------
async def test_a_model_a_service_says_it_does_not_have_is_not_asked_twice(
    settings, monkeypatch
) -> None:
    """OpenRouter was asked for a Google-shaped id seven times in forty-five
    seconds and rejected every one, because nothing remembered the first no."""
    from astolfo.llm import LLMClient

    asked: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        import json as _json

        asked.append(_json.loads(request.content)["model"])
        return httpx.Response(
            400, json={"error": {"message": "models/gemini-2.5-flash is not a valid model ID"}}
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(
        settings.replace(providers=["openrouter"]), transport=httpx.MockTransport(refuse)
    )
    for _ in range(3):
        await client.chat([{"role": "user", "content": "hi"}], model="models/gemini-2.5-flash")
    await client.aclose()

    assert asked.count("models/gemini-2.5-flash") == 1, asked


async def test_a_400_about_the_request_still_says_nothing_about_the_model(
    settings, monkeypatch
) -> None:
    """Only "we do not have that model" is a durable fact. A complaint about the
    request's shape is this turn's problem."""
    from astolfo.llm import LLMClient

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(400, json={"error": {"message": "temperature must be <= 2"}})

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(
        settings.replace(providers=["openrouter"]), transport=httpx.MockTransport(refuse)
    )
    await client.chat([{"role": "user", "content": "hi"}], model="some/model")
    stuck = ("openrouter", "some/model") in client._unknown
    await client.aclose()

    assert not stuck


# -- and the strike that was counted four times in forty seconds -----------
def test_a_model_used_while_resting_is_not_struck_again(settings, monkeypatch) -> None:
    """It went from strike seven to strike ten in forty seconds, all of them the
    same model being used because it was the only one left."""
    from astolfo.llm import LLMClient

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(
        settings.replace(providers=["openrouter"], free_mode=True),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"data": []} if r.url.path.endswith("/models") else {}
            )
        ),
    )
    client.mark_unusable("only/model")
    for _ in range(5):
        client.mark_unusable("only/model")

    assert client._strikes["only/model"] == 1
