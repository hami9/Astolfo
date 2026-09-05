"""The bot went quiet in a private chat while its commands kept working.

Found in the live database: the owner's own chat carried `dormant = 1`. Nothing
in the panel had been pressed - `send_reply` had switched it off after Telegram
refused one message, using a rule written for groups.

It reads as the bot having gone mute rather than as a switch, because commands
have their own handler and keep answering. That is what makes it worth its own
test file.
"""

from __future__ import annotations

from astolfo import chat as chat_mod
from astolfo.chat import send_reply
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update


class _Refuses(FakeMessage):
    """Telegram saying no, in the words it uses for a group it cannot post in."""

    def __init__(self, reason: str, **kwargs) -> None:
        super().__init__("hi", **kwargs)
        self._reason = reason

    async def reply_text(self, *args, **kwargs):
        raise RuntimeError(self._reason)


async def run(rt, message):
    await chat_mod.handle_message(make_update(message), FakeContext(rt, FakeBot()))


# -- what the rule is for --------------------------------------------------
async def test_a_group_that_will_not_take_a_reply_is_still_switched_off(rt) -> None:
    """The 2.5.3 fix, unchanged: twenty-one model calls into a group that could
    not hear any of them."""
    message = _Refuses("Not enough rights to send text messages", chat_id=-100)
    await send_reply(message, "hello", rt)

    assert -100 in rt.dormant


# -- and where it does not belong ------------------------------------------
async def test_a_private_chat_is_never_switched_off_for_it(rt) -> None:
    """There is no permission to grant in a private chat, so there is nothing
    for anybody to fix and turn back on."""
    message = _Refuses(
        "Forbidden: bot was blocked by the user", chat_type="private", chat_id=777
    )
    await send_reply(message, "hello", rt)

    assert 777 not in rt.dormant


async def test_the_group_wording_in_a_private_chat_changes_nothing(rt) -> None:
    """Even if Telegram used the group phrasing, a private chat has no rights."""
    message = _Refuses(
        "Not enough rights to send text messages", chat_type="private", chat_id=778
    )
    await send_reply(message, "hello", rt)

    assert 778 not in rt.dormant


async def test_a_private_chat_that_was_switched_off_still_answers_once_it_is_back(
    rt, llm
) -> None:
    """The way out, so the state found on the server is recoverable: the panel
    switch is the same switch, and it works in both directions."""
    rt.set_chat_off(779, True)
    llm.reply = "yahoo~"
    quiet = FakeMessage("سلام", chat_type="private", chat_id=779)
    await run(rt, quiet)

    assert not quiet.sent, "off means off"

    rt.set_chat_off(779, False)
    again = FakeMessage("سلام", chat_type="private", chat_id=779)
    await run(rt, again)

    assert again.sent == ["yahoo~"]


async def test_an_ordinary_private_chat_answers(rt, llm) -> None:
    llm.reply = "yahoo~ what's up?"
    message = FakeMessage("سلام خوبی؟", chat_type="private", chat_id=780)
    await run(rt, message)

    assert message.sent == ["yahoo~ what's up?"]


# -- and the way out that looked like it worked ----------------------------
async def test_unmute_brings_a_switched_off_chat_back(rt, llm) -> None:
    """Screenshot from the server: /unmute answered "I'm baaack! 🎉" and the
    chat stayed silent. Muted and switched off are separate flags and only one
    of them had a command, so the bot said something untrue about itself."""
    from astolfo import commands
    from tests.conftest import FakeContext, FakeMessage, make_update

    rt.set_chat_off(781, True)
    rt.store.get(781).muted = True

    said = FakeMessage("/unmute", chat_type="private", chat_id=781, user_id=1)
    await commands.unmute(make_update(said), FakeContext(rt, FakeBot()))

    assert said.sent, "it answers"
    assert 781 not in rt.dormant, "and it means it"
    assert not rt.store.get(781).muted

    llm.reply = "yahoo~"
    after = FakeMessage("سلام", chat_type="private", chat_id=781)
    await run(rt, after)

    assert after.sent == ["yahoo~"]


async def test_status_says_when_a_chat_is_switched_off(rt) -> None:
    """Nothing anywhere said so. A chat in that state answers commands and
    nothing else, which reads as the bot being broken."""
    from astolfo import commands
    from tests.conftest import FakeContext, FakeMessage, make_update

    rt.set_chat_off(782, True)
    said = FakeMessage("/status", chat_type="private", chat_id=782, user_id=1)
    await commands.status(make_update(said), FakeContext(rt, FakeBot()))

    assert "switched off" in said.sent[-1] or "خاموش" in said.sent[-1], said.sent


async def test_status_says_nothing_extra_when_the_chat_is_on(rt) -> None:
    from astolfo import commands
    from tests.conftest import FakeContext, FakeMessage, make_update

    said = FakeMessage("/status", chat_type="private", chat_id=783, user_id=1)
    await commands.status(make_update(said), FakeContext(rt, FakeBot()))

    assert "switched off" not in said.sent[-1] and "خاموش" not in said.sent[-1]
