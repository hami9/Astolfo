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

# The optional arrow is the transcript notation the prompt uses for a reply
# ("Sara → Reza: ..."). Small models copy the shape of what they were shown, so
# "Astolfo → Sara:" comes back often enough to be worth stripping.
_NAME_PREFIX = re.compile(
    r"^\s*(astolfo|آستولفو|استولفو)\s*(?:(?:→|->)\s*[^\n:]{1,32})?\s*[:：\-–]\s*", re.I
)
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

# Persian typed on a phone arrives in several spellings of the same word: an
# Arabic keyboard gives ي and ك where Persian wants ی and ک, diacritics and
# kashida survive copy-paste, and Arabic-Indic digits are a different codepoint
# from the ones a model was mostly trained on. A small model reads each variant
# as a different token, which is exactly when it starts sounding stupid.
_ARABIC_FORMS = str.maketrans(
    {
        "ي": "ی", "ى": "ی", "ﻯ": "ی", "ﻰ": "ی",
        "ك": "ک", "ﻙ": "ک", "ﻚ": "ک",
        "ة": "ه", "ۀ": "ه",
        "أ": "ا", "إ": "ا", "ٱ": "ا",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        # Persian punctuation stays as it is: it is part of writing Persian, and
        # replacing it would nudge the reply into the wrong register.
        # Kashida and the invisible marks a phone keyboard sprinkles in. The
        # zero-width non-joiner is deliberately absent: it separates words in
        # Persian, so dropping it would glue them together.
        "ـ": "", "​": "", "‍": "", "‎": "", "‏": "",
        "﻿": "", "­": "",
    }
)
_HARAKAT = re.compile("[ً-ْٰـ]")
_ZWNJ_RUN = re.compile("‌{2,}")
# Three of the same letter is someone stretching a word; the meaning is in the
# first two. Four of anything else is decoration. Digits are left alone, because
# 1000 is a number rather than an enthusiastic 100.
_LETTER_RUN = re.compile(r"([^\W\d_])\1{2,}", re.UNICODE)
_OTHER_RUN = re.compile(r"([^\w\s])\1{3,}", re.UNICODE)
_SPACES = re.compile(r"[ \t ]{2,}")
_MANY_LINES = re.compile(r"\n{3,}")


def normalize_input(text: str) -> str:
    """Fold the many spellings of a Persian message into one.

    This runs on the way to the model, never on the way back: what the chat sees
    is what the person typed. It is also the cheapest token saving there is,
    because a stretched "سلاااااام" and a kashida are tokens paid for and
    understood by nobody.
    """
    body = (text or "").strip()
    if not body:
        return ""
    body = body.translate(_ARABIC_FORMS)
    body = _HARAKAT.sub("", body)
    body = _ZWNJ_RUN.sub("‌", body)
    body = _LETTER_RUN.sub(r"\1\1", body)
    body = _OTHER_RUN.sub(r"\1\1\1", body)
    body = _SPACES.sub(" ", body)
    return _MANY_LINES.sub("\n\n", body).strip()


def shorten(text: str, limit: int) -> str:
    """Cut to a length a model will not choke on, on a word boundary if there is one."""
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    cut = body.rfind(" ", 0, limit)
    return body[: cut if cut > limit // 2 else limit].rstrip() + "…"


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

    # Normalised, so being called by name still works when the name is typed on
    # an Arabic keyboard or stretched out.
    text = normalize_input(message.text or message.caption or "").lower()
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
