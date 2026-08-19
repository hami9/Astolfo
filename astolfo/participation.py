"""How talkative the bot is right now.

Three modes. **manual** answers only when it is spoken to. **auto** also jumps
into conversations on its own. **smart** is auto until that stops being a good
idea — the allowance is running out, every service is resting, or the chat is
moving so fast that an uninvited reply is noise — and manual until it passes.

A group can set its own; otherwise the global one applies.
"""

from __future__ import annotations

import logging
import random
import time

log = logging.getLogger(__name__)

MANUAL = "manual"
AUTO = "auto"
SMART = "smart"
MODES = (MANUAL, AUTO, SMART)

# A chat busier than this many messages a minute is having its own conversation.
BUSY_PER_MINUTE = 12
# Below this share of the daily budget left, smart mode stops volunteering.
TIGHT_BUDGET = 0.7


def normalize(value: str | None) -> str:
    choice = (value or "").strip().lower()
    return choice if choice in MODES else ""


def mode_for(rt, state) -> str:
    """The mode this chat is running under, its own beating the global one."""
    return normalize(state.mode) or normalize(rt.settings.reply_mode) or SMART


def effective(rt, state) -> tuple[str, str]:
    """What the bot will actually do, and why. Smart resolves to manual or auto."""
    mode = mode_for(rt, state)
    if mode != SMART:
        return mode, "set here" if normalize(state.mode) else "set globally"

    if not rt.llm.usable_now():
        return MANUAL, "every service is resting"

    budget = rt.settings.daily_budget_usd
    if budget > 0 and rt.budget.today_cost() / budget >= TIGHT_BUDGET:
        return MANUAL, "most of today's budget is spent"

    if state.pace() > BUSY_PER_MINUTE:
        return MANUAL, "the chat is busy right now"

    return AUTO, "quiet enough to join in"


def should_join(rt, state, kind: str) -> bool:
    """Whether to answer a message that was not addressed to the bot."""
    settings = rt.settings
    if time.monotonic() - state.last_reply_at < settings.reply_cooldown:
        return False

    mode, _reason = effective(rt, state)
    if mode == MANUAL:
        return False

    base = state.reply_chance if state.reply_chance is not None else settings.group_reply_chance
    if base <= 0:
        return False
    chance = max(base, settings.media_reply_chance) if kind else base
    return random.random() < chance  # noqa: S311 - a coin flip, not a secret
