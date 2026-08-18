"""Small helpers shared by the panel sections."""

from __future__ import annotations

import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

PREFIX = "ap"  # every callback the panel owns starts with this


def cb(*parts: object) -> str:
    """Callback data. Telegram allows 64 bytes, so the parts stay short."""
    return ":".join([PREFIX, *(str(p) for p in parts)])


def button(label: str, *parts: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=cb(*parts))


def keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([row for row in rows if row])


def back_row(*parts: object) -> list[InlineKeyboardButton]:
    return [button("‹ back", *parts)] if parts else [button("‹ back", "home")]


def confirm_rows(label: str, *parts: object) -> list[list[InlineKeyboardButton]]:
    """A destructive action always costs a second press."""
    return [[button(f"⚠️ yes, {label}", *parts)], back_row()]


def ago(when: float | None) -> str:
    if not when:
        return "never"
    seconds = max(0.0, time.time() - when)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def yes_no(value: object) -> str:
    return "on" if value else "off"


def trim(text: str, limit: int = 28) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
