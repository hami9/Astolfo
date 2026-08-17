"""ابزارهای کمکی: برش پیام، پاک‌سازی خروجی، نشانگر تایپ، تشخیص خطاب."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Iterator, List, Optional

from telegram import Message, User
from telegram.constants import ChatAction

TELEGRAM_MAX_LEN = 3900  # حاشیهٔ امن نسبت به سقف واقعی ۴۰۹۶

_NAME_PREFIX = re.compile(
    r"^\s*(آستولفو|استولفو|astolfo)\s*[:：\-–]\s*", re.I
)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_UNDERLINE = re.compile(r"__(.+?)__", re.S)
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.M)
_AI_ISMS = re.compile(
    r"(به\s*عنوان\s*(یک\s*)?(هوش\s*مصنوعی|مدل\s*زبانی)|"
    r"as\s+an\s+ai\s+(language\s+)?model)",
    re.I,
)


def clean_name(raw: Optional[str]) -> str:
    name = (raw or "کاربر").replace("\n", " ").strip()
    return name[:32] or "کاربر"


def split_message(text: str, limit: int = TELEGRAM_MAX_LEN) -> Iterator[str]:
    """پیام‌های بلندتر از سقف تلگرام را روی مرز خط/فاصله می‌شکند."""
    text = text.strip()
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunk = text[:cut].rstrip()
        if chunk:
            yield chunk
        text = text[cut:].lstrip()


def polish(reply: str) -> str:
    """مارک‌داون و لحن دستیاری را از خروجی پاک می‌کند تا شبیه چت واقعی بماند."""
    text = (reply or "").strip()
    if not text:
        return ""

    text = _NAME_PREFIX.sub("", text)

    # اگر بلوک کد دارد (یعنی کاربر واقعاً کد خواسته) دست نمی‌زنیم
    if "```" not in text:
        text = _BOLD.sub(r"\1", text)
        text = _UNDERLINE.sub(r"\1", text)
        text = _HEADER.sub("", text)
        text = _BULLET.sub("• ", text)

    text = _AI_ISMS.sub("من", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_sources(citations, limit: int = 3) -> str:
    """منابع جست‌وجو را به یک خط کوتاه تبدیل می‌کند."""
    seen, lines = set(), []
    for cite in citations:
        url = getattr(cite, "url", None)
        if not url or url in seen:
            continue
        seen.add(url)
        title = (getattr(cite, "title", "") or url).strip()
        lines.append(f"• {title[:60]} — {url}")
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return "\n\n📎 از این‌ها خوندم:\n" + "\n".join(lines)


@contextlib.asynccontextmanager
async def typing_indicator(bot, chat_id: int, action: str = ChatAction.TYPING):
    """نمایش مداوم «در حال تایپ...» تا پایان تولید پاسخ."""

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


def is_addressed(message: Message, bot_user: User, aliases: Optional[List[str]] = None) -> bool:
    """آیا پیام مستقیماً خطاب به ربات است؟ (ریپلای، منشن یا صدا زدن اسمش)"""
    reply = message.reply_to_message
    if reply and reply.from_user and bot_user and reply.from_user.id == bot_user.id:
        return True

    text = ((message.text or message.caption) or "").lower()

    if bot_user and bot_user.username and f"@{bot_user.username.lower()}" in text:
        return True

    for entity in list(message.entities or []) + list(message.caption_entities or []):
        if entity.type == "text_mention" and entity.user and bot_user and entity.user.id == bot_user.id:
            return True

    for alias in aliases or ["آستولفو", "استولفو", "astolfo"]:
        if alias.lower() in text:
            return True
    return False
