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

# The history reaches the model as "Reza: ..." lines, and a small model reading
# that continues the transcript instead of answering into it: the reply comes
# back wearing the name of whoever it is answering. The prompt says not to, and
# this is what catches the ones that do it anyway.
_SPEAKER = re.compile(
    r"""^\s*
    (?P<who>[^\s:：\n][^:：\n]{0,31}?)      # a short label
    (?:\s*(?:→|->)\s*[^:：\n]{1,32}?)?      # optionally "→ somebody"
    \s*[:：][ \t]+                          # the colon, then real whitespace
    (?=\S)                                  # and something after it
    """,
    re.X,
)
# A label that is really a name: a few words, no sentence punctuation, not a
# clock time. "20:35" and "https://x" must survive; "Arash(IQ 26):" must not.
_NOT_A_NAME = re.compile(r"[.!?،؛…]|^\d+$")
# "assistant:" is not a name, it is a model that has lost track of what it is.
# Left in place on purpose, so the quality guard still sees it and asks another
# model rather than quietly sending the remains.
_ROLE_WORDS = {"system", "user", "assistant", "human", "ai", "bot"}


def strip_speaker(reply: str, known: Iterable[str] = ()) -> str:
    """Drop a leading "Name:" the model copied from the transcript it was shown.

    Names the chat actually contains are removed on sight. Anything else has to
    look like a name rather than like prose, because a false positive eats the
    first words of a real answer.
    """
    body = (reply or "").lstrip()
    match = _SPEAKER.match(body)
    if not match:
        return body
    who = match.group("who").strip()
    rest = body[match.end() :].lstrip()
    if not rest:
        return body

    if who.casefold() in _ROLE_WORDS:
        return body
    folded = {" ".join(name.split()).casefold() for name in known if name}
    if who.casefold() in folded:
        return rest
    # Nobody the chat knows, so it has to look like a name on its own: one word
    # and short. Persian puts a colon after a clause all the time ("راستش
    # نمیدونم: شاید فردا"), and eating the first half of a real answer is a
    # worse failure than leaving one stray label in place.
    if len(who) <= 24 and len(who.split()) == 1 and not _NOT_A_NAME.search(who):
        return rest
    return body


def cut_impersonation(reply: str, known: Iterable[str] = ()) -> str:
    """Stop at the point where it starts writing other people's messages.

    Shown a transcript of "Reza: ..." lines, a small model does not answer into
    it - it continues the script, and one reply comes back carrying two or three
    invented turns with real people's names on them. Putting words in a member's
    mouth is the worst thing this bot can do, so everything from the first such
    line onward is cut and only what it said as itself is sent.
    """
    folded = {" ".join(name.split()).casefold() for name in known if name}
    if not folded:
        return reply
    kept: list[str] = []
    for line in (reply or "").splitlines():
        match = _SPEAKER.match(line)
        if match and match.group("who").strip().casefold() in folded:
            break
        kept.append(line)
    return "\n".join(kept).strip() or reply.strip()
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

# Scripts nobody in a Persian or English chat was writing in. A free multilingual
# model that loses the thread reaches for one of these mid-sentence.
_STRAY_SCRIPT = re.compile(
    "[Ѐ-ӿ"      # Cyrillic
    "֐-׿"       # Hebrew
    "฀-๿"       # Thai
    "ᄀ-ᇿ"       # Hangul
    "぀-ヿ"       # Kana
    "㐀-鿿"       # CJK
    "가-힯]"      # Hangul syllables
)


# Explicit terms, checked only against what the bot itself wrote - never against
# what anybody sends it. The prompt is what makes it deflect gracefully; this is
# the backstop that makes sure the one output that must never ship never ships,
# the same way the leak patterns above work.
#
# Deliberately narrow. Persian "کس" is left out because it is also the ordinary
# word in "هیچ کس" and "کسی", and eating a real reply is its own failure; the
# slang spelling "کص" and the unambiguous terms are what this catches.
_EXPLICIT = re.compile(
    r"(کیر|کص|کوس|ممه|پستون|کون\b|جنده|بگام|میگام|گایید|ساک\s*بزن|سکس"
    r"|\bcock\b|\bdick\b|\bpussy\b|\bcunt\b|\btits\b|\bboobs\b|blowjob"
    r"|\bhorny\b|\bcum\b|suck\s+(my|your)|fuck\s+(me|you)\b)",
    re.I,
)


def went_explicit(reply: str) -> bool:
    """Whether the bot wrote something sexual it should have deflected instead.

    Astolfo is unbothered by crude jokes and the group makes plenty; what this
    catches is the bot joining in - describing itself, agreeing to something, or
    answering a question built out of a real member's name. It reads the reply
    only. Nothing anybody sends is filtered, blocked or judged by it.
    """
    return bool(_EXPLICIT.search(reply or ""))


def stray_language(reply: str) -> str | None:
    """Which foreign script leaked into this reply, if any.

    Free models drift: a Persian answer comes back with a Chinese or Cyrillic word
    dropped into the middle of it. A Latin word is left alone here because Persian
    chats really do use English terms; only scripts nobody was writing in count.
    """
    match = _STRAY_SCRIPT.search(reply or "")
    return match.group(0) if match else None

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

    stray = stray_language(body)
    if stray:
        return f"drifted into another script ({stray!r})"

    folded = " ".join(body.lower().split())
    if echoes and folded == " ".join(echoes.lower().split()):
        return "echoed the message"
    if previous and folded == " ".join(previous.lower().split()):
        return "repeated its previous reply"
    return None
