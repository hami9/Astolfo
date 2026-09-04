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

from . import interest as interest_mod

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


def should_join(
    rt,
    state,
    kind: str,
    *,
    text: str = "",
    in_thread: bool = False,
    spoke_last: bool | None = None,
) -> bool:
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
    # One train of thought: while another chat has its attention, this one gets a
    # fraction of the usual eagerness. Being spoken to is unaffected - that path
    # never reaches here.
    chance *= rt.attention.share_for(state.chat_id)

    if not settings.interest_scoring:
        return random.random() < chance  # noqa: S311 - a coin flip, not a secret

    interest = interest_mod.rate(
        text,
        has_media=bool(kind),
        in_thread=in_thread,
        spoke_last=state.awaiting_reply if spoke_last is None else spoke_last,
        notes=state.notes,
    )
    joining = interest_mod.worth_joining(interest, chance)
    log.debug(
        "chat %s: interest %.2f (%s) -> %s", state.chat_id, interest.score,
        interest.reason, "joining" if joining else "staying out",
    )
    return joining
