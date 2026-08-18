"""Message post-processing and Telegram helpers."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Iterable, Iterator

from telegram import Message, User
from telegram.constants import ChatAction

TELEGRAM_MAX_LEN = 3900  # safety margin below the real 4096 limit
DEFAULT_ALIASES = ("astolfo", "آستولفو", "استولفو")

_NAME_PREFIX = re.compile(r"^\s*(astolfo|آستولفو|استولفو)\s*[:：\-–]\s*", re.I)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_UNDERLINE = re.compile(r"__(.+?)__", re.S)
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.M)
_AI_ISM = re.compile(
    r"(به\s*عنوان\s*(یک\s*)?(هوش\s*مصنوعی|مدل\s*زبانی)|as\s+an\s+ai\s+(language\s+)?model)",
    re.I,
)
_BLANK_LINES = re.compile(r"\n{3,}")

# Small models sometimes echo the scaffolding instead of answering through it.
_PROMPT_LEAK = re.compile(
    r"(<identity>|</identity>|<voice>|<canon-anchors|<never>|<truthfulness"
    r"|<response-mode|<chat-context|<examples>|<output>|<media>"
    r"|absolute rules:|how you write:)",
    re.I,
)
_ROLE_PREFIX = re.compile(r"^\s*(system|user|assistant)\s*:", re.I)


def clean_name(raw: str | None) -> str:
    name = (raw or "user").replace("\n", " ").strip()
    return name[:32] or "user"


def split_message(text: str, limit: int = TELEGRAM_MAX_LEN) -> Iterator[str]:
    """Split on line or word boundaries so no chunk exceeds Telegram's limit."""
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunk = remaining[:cut].rstrip()
        if chunk:
            yield chunk
        remaining = remaining[cut:].lstrip()


def polish(reply: str) -> str:
    """Strip markdown, name prefixes and assistant-speak so replies read like chat."""
    text = (reply or "").strip()
    if not text:
        return ""

    text = _NAME_PREFIX.sub("", text)
    if "```" not in text:  # leave real code blocks alone
        text = _BOLD.sub(r"\1", text)
        text = _UNDERLINE.sub(r"\1", text)
        text = _HEADER.sub("", text)
        text = _BULLET.sub("• ", text)
    text = _AI_ISM.sub("I", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def format_sources(citations: Iterable, limit: int = 3) -> str:
    seen, lines = set(), []
    for citation in citations:
        url = getattr(citation, "url", None)
        if not url or url in seen:
            continue
        seen.add(url)
        title = (getattr(citation, "title", "") or url).strip()
        lines.append(f"• {title[:60]} — {url}")
        if len(lines) >= limit:
            break
    return "\n\n📎 sources:\n" + "\n".join(lines) if lines else ""


def is_addressed(
    message: Message, bot_user: User, aliases: Iterable[str] = DEFAULT_ALIASES
) -> bool:
    """True when the message replies to, mentions, or names the bot."""
    reply = message.reply_to_message
    if reply and reply.from_user and bot_user and reply.from_user.id == bot_user.id:
        return True

    text = (message.text or message.caption or "").lower()
    if bot_user and bot_user.username and f"@{bot_user.username.lower()}" in text:
        return True

    entities: list = list(message.entities or []) + list(message.caption_entities or [])
    for entity in entities:
        mentioned = entity.type == "text_mention" and entity.user and bot_user
        if mentioned and entity.user.id == bot_user.id:
            return True

    return any(alias.lower() in text for alias in aliases)


@contextlib.asynccontextmanager
async def typing_indicator(bot, chat_id: int, action: str = ChatAction.TYPING):
    """Keep the typing status alive until the reply is ready."""

    async def pulse() -> None:
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id=chat_id, action=action)
            await asyncio.sleep(4.5)

    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def looks_broken(reply: str, *, echoes: str = "", previous: str = "") -> str | None:
    """Why this reply is unusable, or None when it is fine.

    Weak models fail in recognisable ways: they quote the prompt back, answer in
    the transcript format they were shown, repeat the question, or repeat their own
    last line. Each is worth another model rather than sending to the chat.
    """
    body = (reply or "").strip()
    if not body:
        return "empty"
    if _PROMPT_LEAK.search(body):
        return "leaked the prompt"
    if _ROLE_PREFIX.match(body):
        return "answered in transcript format"

    folded = " ".join(body.lower().split())
    if echoes and folded == " ".join(echoes.lower().split()):
        return "echoed the message"
    if previous and folded == " ".join(previous.lower().split()):
        return "repeated its previous reply"
    return None
