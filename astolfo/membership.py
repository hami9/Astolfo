"""Keeping track of which chats the bot is actually in."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from . import runtime

log = logging.getLogger(__name__)

PRESENT = {"member", "administrator", "creator", "restricted"}


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram reports every add and removal here, including silent ones."""
    change = update.my_chat_member
    if change is None:
        return

    chat = change.chat
    status = change.new_chat_member.status
    db = runtime.get(context).db

    if status in PRESENT:
        db.joined_chat(
            chat.id,
            kind=chat.type,
            title=chat.title or chat.full_name or "",
            username=chat.username or "",
        )
        log.info("added to %s (%s)", chat.title or chat.id, chat.type)
    else:
        db.left_chat(chat.id)
        log.info("removed from %s (%s)", chat.title or chat.id, status)
