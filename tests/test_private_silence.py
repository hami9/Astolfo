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
