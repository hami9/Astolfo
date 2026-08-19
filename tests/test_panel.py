"""The owner's panel: what it shows, what it changes, and who it ignores."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from astolfo import runtime as runtime_mod
from astolfo.admin import on_button, on_text, open_panel
from astolfo.runtime import Runtime
from tests.conftest import FakeBot, FakeContext, FakeMessage, FakeQuery, make_press, make_update

MASTER = 4242


class _Client:
    """No network during a reconfigure."""

    def __init__(self, settings, registry=None) -> None:
        self.settings = settings
        self.registry = registry
        self.providers = [
            SimpleNamespace(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                models=[],
                credentials=[],
            )
        ]
        self.probed: list[str] = []

    def usable_now(self) -> bool:
        return True

    async def load_catalog(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def probe(self, name: str) -> tuple[bool, str]:
        self.probed.append(name)
        return True, "answered by test/model"

    def resolve(self, model, **kwargs):
        return model


@pytest.fixture
def owned(settings, monkeypatch) -> Runtime:
    """A bot that already knows its owner, configured the way a server would."""
    monkeypatch.setattr(runtime_mod, "LLMClient", _Client)
    # Through the environment, not the object: reloading settings re-reads it, and
    # that is exactly what a panel change does.
    monkeypatch.setenv("MASTER_ID", str(MASTER))
    monkeypatch.setenv("DATA_DIR", settings.data_dir)
    return Runtime.build(settings.replace(master_id=MASTER))


def _private(text: str = "/panel", user_id: int = MASTER) -> FakeMessage:
    return FakeMessage(text, chat_id=user_id, chat_type="private", user_id=user_id, name="Owner")


async def _press(rt, data: str, *, user_id: int = MASTER, bot: FakeBot | None = None):
    message = _private(user_id=user_id)
    query = FakeQuery(data, message, message.from_user)
    context = FakeContext(rt, bot or FakeBot())
    await on_button(make_press(query), context)
    return query, context


async def _say(rt, text: str, context, *, user_id: int = MASTER):
    message = _private(text, user_id=user_id)
    with pytest.raises(ApplicationHandlerStop):
        await on_text(make_update(message), context)
    return message


# -- access ---------------------------------------------------------------
async def test_the_owner_gets_the_panel(owned):
    message = _private()
    await open_panel(make_update(message), FakeContext(owned, FakeBot()))
    assert "control panel" in message.sent[0]


async def test_anyone_else_gets_nothing(owned):
    message = _private(user_id=99)
    await open_panel(make_update(message), FakeContext(owned, FakeBot()))
    assert message.sent == [], "not even an error: a reply would confirm it exists"


async def test_the_panel_does_not_open_in_a_group(owned):
    message = FakeMessage("/panel", chat_id=-100, user_id=MASTER)
    await open_panel(make_update(message), FakeContext(owned, FakeBot()))
    assert message.sent == []


async def test_a_forwarded_button_does_not_work_for_a_stranger(owned):
    """The buttons travel with the message, so every press is checked again."""
    query, _ = await _press(owned, "ap:keys", user_id=99)
    assert query.edits == []
    assert query.answers == ["not for you"]


# -- services and keys ----------------------------------------------------
async def test_a_key_is_stored_encrypted_and_the_message_deleted(owned):
    _query, context = await _press(owned, "ap:svc:s:google:addkey")

    message = _private("g-secret-key")
    with pytest.raises(ApplicationHandlerStop):
        await on_text(make_update(message), context)

    assert message.deleted, "the key must not stay in the chat history"
    stored = owned.db.credentials("google")
    assert len(stored) == 1
    assert bytes(stored[0]["value"]) != b"g-secret-key", "not in the clear"
    assert owned.box.decrypt(bytes(stored[0]["value"])) == "g-secret-key"


async def test_a_key_can_be_labelled_when_it_is_added(owned):
    _query, context = await _press(owned, "ap:svc:s:groq:addkey")
    message = _private("work laptop: gsk-the-key")
    with pytest.raises(ApplicationHandlerStop):
        await on_text(make_update(message), context)

    stored = owned.db.credentials("groq")[0]
    assert stored["label"] == "work laptop"
    assert owned.box.decrypt(bytes(stored["value"])) == "gsk-the-key"


async def test_a_key_is_never_shown_in_full(owned):
    owned.registry.add_key("google", "AIza-0000000000secret", label="main")
    key_id = owned.db.credentials("google")[0]["id"]

    query, _ = await _press(owned, f"ap:svc:k:{key_id}")
    assert "0000000000secret" not in query.edits[0]
    assert "AIza-" in query.edits[0]


async def test_testing_a_service_asks_it(owned):
    query, _ = await _press(owned, "ap:svc:s:openrouter:test")
    assert owned.llm.probed == ["openrouter"]
    assert "answered by" in query.edits[0]


async def test_removing_a_key_needs_a_second_press(owned):
    owned.registry.add_key("google", "AIza-key")
    key_id = owned.db.credentials("google")[0]["id"]

    query, _ = await _press(owned, f"ap:svc:k:{key_id}:rm")
    assert "Remove this key" in query.edits[0]
    assert owned.db.credentials("google"), "still there until confirmed"

    await _press(owned, f"ap:svc:k:{key_id}:rm!")
    assert owned.db.credentials("google") == []


# -- settings -------------------------------------------------------------
async def test_flipping_a_switch_takes_effect_immediately(owned):
    assert owned.settings.free_mode is False
    await _press(owned, "ap:cfg:flip:free_mode")
    assert owned.settings.free_mode is True, "no restart needed"
    assert owned.db.overrides()["free_mode"] == "1"


async def test_a_typed_setting_is_validated(owned):
    _query, context = await _press(owned, "ap:cfg:edit:max_history")
    message = await _say(owned, "plenty", context)
    assert "does not accept" in message.sent[0]
    assert owned.db.overrides() == {}


async def test_a_setting_can_be_reset_to_the_env_value(owned):
    _query, context = await _press(owned, "ap:cfg:edit:group_reply_chance")
    await _say(owned, "0.9", context)
    assert owned.settings.group_reply_chance == 0.9

    _query, context = await _press(owned, "ap:cfg:reset")
    await _say(owned, "group_reply_chance", context)
    assert owned.settings.group_reply_chance == 0.3


# -- groups and people ----------------------------------------------------
def _busy_group(rt, chat_id: int = -100) -> None:
    rt.db.joined_chat(chat_id, kind="supergroup", title="Test Group")
    rt.db.seen_chat(chat_id, kind="supergroup", title="Test Group")
    rt.db.seen_member(user_id=7, chat_id=chat_id, name="Reza", username="reza")


async def test_the_groups_the_bot_is_in_are_listed(owned):
    _busy_group(owned)
    query, _ = await _press(owned, "ap:chats")
    assert "Test Group" in query.edits[0]


async def test_a_group_can_be_muted_from_the_panel(owned):
    _busy_group(owned)
    await _press(owned, "ap:chat:-100:mute:1")
    assert owned.store.get(-100).muted is True
    assert owned.db.chat(-100)["muted"] == 1


async def test_leaving_a_group_needs_a_second_press(owned):
    _busy_group(owned)
    bot = FakeBot()
    query, _ = await _press(owned, "ap:chat:-100:leave", bot=bot)
    assert bot.left == []
    assert "Leave this group?" in query.edits[0]

    await _press(owned, "ap:chat:-100:leave!", bot=bot)
    assert bot.left == [-100]
    assert owned.db.chat(-100)["left_at"] is not None


async def test_blocking_someone_makes_the_bot_ignore_them(owned):
    _busy_group(owned)
    await _press(owned, "ap:ppl:7:block:1")
    assert 7 in owned.blocked
    assert owned.db.user(7)["blocked"] == 1

    await _press(owned, "ap:ppl:7:block:0")
    assert owned.blocked == set()


async def test_people_can_be_found_by_name(owned):
    _busy_group(owned)
    _query, context = await _press(owned, "ap:ppl:find")
    message = await _say(owned, "Reza", context)
    assert "id 7" in message.sent[0] or "Reza" in message.sent[0]


# -- data -----------------------------------------------------------------
async def test_the_database_can_be_downloaded(owned):
    bot = FakeBot()
    await _press(owned, "ap:data:backup", bot=bot)
    assert bot.documents == ["astolfo.db"]


async def test_every_change_lands_in_the_audit_trail(owned):
    _busy_group(owned)
    await _press(owned, "ap:chat:-100:mute:1")
    query, _ = await _press(owned, "ap:data:audit")
    assert "mute" in query.edits[0]


# -- robustness -----------------------------------------------------------
async def test_a_broken_button_does_not_take_the_bot_down(owned):
    query, _ = await _press(owned, "ap:chat:not-a-number:mute:1")
    assert query.edits, "the panel answers with an error instead of crashing"


async def test_an_ordinary_private_message_still_reaches_the_chat_pipeline(owned):
    """Only a pending prompt swallows a message; nothing else does."""
    context = FakeContext(owned, FakeBot())
    message = _private("hello astolfo")
    await on_text(make_update(message), context)  # must not raise
    assert message.sent == []


# -- limits and modes -----------------------------------------------------
async def test_a_group_can_be_told_to_answer_only_when_spoken_to(owned):
    _busy_group(owned)
    await _press(owned, "ap:chat:-100:mode:manual")

    assert owned.store.get(-100).mode == "manual"
    assert owned.db.chat(-100)["mode"] == "manual"

    await _press(owned, "ap:chat:-100:mode:-")
    assert owned.store.get(-100).mode == "", "back to following the global mode"


async def test_a_group_gets_its_own_daily_limit(owned):
    _busy_group(owned)
    _query, context = await _press(owned, "ap:chat:-100:limit")
    await _say(owned, "40", context)

    assert owned.store.get(-100).daily_limit == 40
    assert owned.db.chat(-100)["daily_limit"] == 40


async def test_a_limit_that_is_not_a_number_is_refused(owned):
    _busy_group(owned)
    _query, context = await _press(owned, "ap:chat:-100:limit")
    message = await _say(owned, "plenty", context)

    assert "not a number" in message.sent[0]
    assert owned.store.get(-100).daily_limit == 0


async def test_every_group_can_be_set_from_one_press(owned):
    for chat_id in (-100, -200):
        owned.db.joined_chat(chat_id, kind="supergroup", title=f"Group {chat_id}")

    query, _ = await _press(owned, "ap:chats:all:manual")

    assert "2 group(s)" in (query.answers[0] or "")
    assert all(row["mode"] == "manual" for row in owned.db.list_chats())


async def test_a_person_gets_their_own_daily_limit(owned):
    _busy_group(owned)
    _query, context = await _press(owned, "ap:ppl:7:limit")
    await _say(owned, "5", context)

    assert owned.limit_for(7) == 5
    assert owned.db.user(7)["daily_limit"] == 5
