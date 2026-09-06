"""Shared fixtures and lightweight fakes. No network, no Telegram credentials."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

from astolfo.config import Settings  # noqa: E402
from astolfo.llm import ChatResult, Usage  # noqa: E402
from astolfo.runtime import Runtime  # noqa: E402


class FakeLLM:
    """Stands in for LLMClient; records calls and returns canned replies."""

    def __init__(
        self,
        reply: str | None = "yahoo~ what's up?",
        citations=None,
        usage: Usage | None = None,
    ):
        self.reply = reply
        self.citations = citations or []
        self.usage = usage or Usage(prompt_tokens=100, completion_tokens=20, cost=0.0004)
        self.calls: list[dict] = []
        self.json_calls: list[dict] = []
        self.json_result: dict | None = None
        self.providers = [SimpleNamespace(name="openrouter")]
        self.reachable = True
        self.rested: list[str] = []

    def resolve(self, model: str, *, vision: bool = False, audio: bool = False) -> str:
        return model

    def context_window(self, model: str) -> int:
        return 0  # unknown, so the configured budget is used as written

    def stuck_on(self, model: str) -> bool:
        # The real client answers this from the live pool. A fake with an endless
        # supply of models is never down to its last one.
        return False

    def mark_unusable(self, model: str, seconds: float | None = None) -> None:
        # The retry path rests a model that answered badly. Recorded rather than
        # ignored: a double that quietly swallows a call the real client depends
        # on is how a broken path looks tested.
        self.rested.append(model)

    def usable_now(self) -> bool:
        # A failing model is not the same as an unreachable service; a test that
        # wants "everything is down" sets this to False itself.
        return self.reachable

    def throttled_for(self) -> float:
        return 0.0

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.reply is None:
            return ChatResult(error="boom", usage=Usage())
        return ChatResult(
            text=self.reply, model=kwargs.get("model", "fake"),
            service="openrouter", latency_ms=42,
            citations=self.citations, usage=self.usage,
        )

    async def json_chat(self, messages, **kwargs):
        self.json_calls.append({"messages": messages, **kwargs})
        return self.json_result, Usage(prompt_tokens=30, completion_tokens=10, cost=0.00001)

    async def load_catalog(self):
        return None

    async def aclose(self):
        return None


class FakeBot:
    def __init__(self, file_bytes: bytes | None = None):
        self.bot = SimpleNamespace(id=999, username="astolfo_bot", first_name="Astolfo")
        self.actions = 0
        self._file_bytes = file_bytes
        self.left: list[int] = []
        self.documents: list[str] = []
        # What getChatAdministrators answers: a list, or an exception to raise.
        self.administrators: object = []

    async def send_chat_action(self, **kwargs):
        self.actions += 1

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)

    async def get_chat_administrators(self, chat_id):
        if isinstance(self.administrators, Exception):
            raise self.administrators
        return self.administrators

    async def send_document(self, chat_id, document, filename=None, **kwargs):
        self.documents.append(filename or "")

    async def get_file(self, file_id):
        payload = self._file_bytes

        class _File:
            async def download_to_drive(self, custom_path=None, **kwargs):
                with open(custom_path, "wb") as fh:
                    fh.write(payload or b"")

        return _File()


class FakeMessage:
    def __init__(
        self,
        text: str | None = None,
        *,
        chat_id: int = -100,
        chat_type: str = "supergroup",
        user_id: int = 1,
        name: str = "Reza",
        is_bot: bool = False,
        reply_to=None,
        caption: str | None = None,
    ):
        self.message_id = 1
        self.text = text
        self.caption = caption
        self.chat = SimpleNamespace(
            id=chat_id, type=chat_type, title="Test Group", username=None
        )
        self.chat_id = chat_id
        self.from_user = SimpleNamespace(id=user_id, is_bot=is_bot, first_name=name, username=None)
        self.reply_to_message = reply_to
        self.entities = []
        self.caption_entities = []
        self.deleted = False
        self.photo = None
        self.sticker = self.animation = self.video = None
        self.video_note = self.voice = self.audio = self.document = None
        self.sent: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=2)

    async def delete(self):
        self.deleted = True


class FakeQuery:
    """A pressed inline button, with the screen it lives on."""

    def __init__(self, data: str, message: FakeMessage, user):
        self.data = data
        self.message = message
        self.from_user = user
        self.answers: list[str | None] = []
        self.edits: list[str] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
        self.message.sent.append(text)


class FakeContext:
    def __init__(self, rt: Runtime, bot: FakeBot):
        self.bot = bot
        self.args: list[str] = []
        self.user_data: dict = {}
        self.application = SimpleNamespace(
            bot_data={"runtime": rt},
            create_task=lambda coro: _drain(coro),
        )


def _drain(coro):
    """Run background coroutines eagerly so tests stay deterministic."""
    import asyncio

    return asyncio.ensure_future(coro)


def make_update(message: FakeMessage) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=message,
        effective_chat=message.chat,
        effective_user=message.from_user,
        callback_query=None,
    )


def make_press(query: FakeQuery) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=query.message,
        effective_chat=query.message.chat,
        effective_user=query.from_user,
        callback_query=query,
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.from_env().replace(
        data_dir=str(tmp_path),
        router_llm=False,
        summaries=False,
        reply_cooldown=0.0,
        response_cache=False,
        keepalive=False,
    )


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def rt(settings, llm) -> Runtime:
    instance = Runtime.build(settings)
    instance.llm = llm
    instance.router._llm = llm
    return instance


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()
