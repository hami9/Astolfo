"""Which chat has the bot's attention right now.

One bot in twenty groups used to behave like twenty bots: each chat rolled its own
dice and it would happily hold four unprompted conversations at once, in none of
them well. A person cannot do that, and pretending to is both the least human thing
it does and a straight multiplier on the bill.

So there is one train of thought. Joining a conversation on its own initiative
claims it for a while; while another chat holds it, this one stays quiet unless it
is actually spoken to. Being addressed always wins - going silent because another
group is livelier would be worse than the problem.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# How much of the join chance is left to a chat that does not hold the attention.
# Not zero: it should still be able to wander back in, just much less eagerly.
DISTRACTED_SHARE = 0.15


class Attention:
    """Where the bot is looking, and for how much longer."""

    def __init__(self, hold: float = 90.0) -> None:
        self.hold = hold
        self.chat_id: int | None = None
        self.since: float = float("-inf")

    def configure(self, hold: float) -> None:
        self.hold = hold

    def claim(self, chat_id: int) -> None:
        """This chat just got an unprompted reply, so it has the attention now."""
        if self.chat_id != chat_id:
            log.debug("attention moves to chat %s", chat_id)
        self.chat_id = chat_id
        self.since = time.monotonic()

    def release(self, chat_id: int) -> None:
        """Being spoken to elsewhere ends the daydream rather than moving it."""
        if self.chat_id == chat_id:
            self.chat_id = None
            self.since = float("-inf")

    def remaining(self) -> float:
        if self.chat_id is None or self.hold <= 0:
            return 0.0
        return max(0.0, self.hold - (time.monotonic() - self.since))

    def holds(self, chat_id: int) -> bool:
        """True when this chat is the one being paid attention to."""
        return self.chat_id == chat_id and self.remaining() > 0

    def elsewhere(self, chat_id: int) -> bool:
        """True when some *other* chat currently holds it."""
        return self.chat_id is not None and self.chat_id != chat_id and self.remaining() > 0

    def share_for(self, chat_id: int) -> float:
        """How much of its usual eagerness this chat gets, in [0, 1]."""
        return DISTRACTED_SHARE if self.elsewhere(chat_id) else 1.0
