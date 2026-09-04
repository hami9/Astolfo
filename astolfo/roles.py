"""Who runs this group, and what the bot itself is in it.

The bot sits in other people's groups. Knowing that the person talking is an admin,
and that it is a plain member with no buttons, changes how it should answer: it can
be useful to whoever runs the place without ever behaving as though it runs it.

Telegram will tell us, but it is a network call, so the answer is cached per chat.
Nothing here grants the bot any power - it is context for the prompt and a set of
rules about what never to do, both of which are the point.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Admin lists change rarely and a stale one is harmless, so this is generous.
TTL = 900.0

MEMBER = "member"
ADMIN = "admin"
OWNER = "owner"


@dataclass
class Roster:
    admins: set[int] = field(default_factory=set)
    owner_id: int | None = None
    bot_is_admin: bool = False
    fetched_at: float = float("-inf")

    def fresh(self, now: float, ttl: float = TTL) -> bool:
        return now - self.fetched_at < ttl

    def role(self, user_id: int) -> str:
        if user_id == self.owner_id:
            return OWNER
        return ADMIN if user_id in self.admins else MEMBER


class Roles:
    """A small per-chat cache of who is in charge."""

    def __init__(self, ttl: float = TTL) -> None:
        self.ttl = ttl
        self._by_chat: dict[int, Roster] = {}

    def cached(self, chat_id: int) -> Roster | None:
        return self._by_chat.get(chat_id)

    def forget(self, chat_id: int) -> None:
        self._by_chat.pop(chat_id, None)

    async def of(self, bot, chat_id: int, *, bot_id: int | None = None) -> Roster:
        """Who runs this chat. Asks Telegram at most once every `ttl` seconds."""
        now = time.monotonic()
        roster = self._by_chat.get(chat_id)
        if roster is not None and roster.fresh(now, self.ttl):
            return roster

        fresh = Roster(fetched_at=now)
        try:
            for member in await bot.get_chat_administrators(chat_id):
                user = getattr(member, "user", None)
                if user is None:
                    continue
                fresh.admins.add(user.id)
                if getattr(member, "status", "") == "creator":
                    fresh.owner_id = user.id
                if bot_id is not None and user.id == bot_id:
                    fresh.bot_is_admin = True
        except Exception as exc:
            # A private chat has no administrators, and a group can refuse. Either
            # way this is decoration on a prompt: keep whatever we had and move on.
            log.debug("could not read the admins of chat %s: %s", chat_id, exc)
            if roster is not None:
                roster.fetched_at = now
                return roster

        self._by_chat[chat_id] = fresh
        return fresh


def standing(roster: Roster | None, *, sender_id: int, sender: str) -> str:
    """One line of prompt saying where everyone stands. Empty when nothing is known."""
    if roster is None or not roster.admins:
        return ""
    lines = []
    role = roster.role(sender_id)
    if role == OWNER:
        lines.append(f"{sender} owns this group.")
    elif role == ADMIN:
        lines.append(f"{sender} is one of this group's admins.")
    lines.append(
        "You have admin rights here but you never use them, and nobody needs to know "
        "you have them."
        if roster.bot_is_admin
        else "You are a plain member here with no powers at all."
    )
    return " ".join(lines)
