"""Is this message worth jumping into?

The old answer was a coin flip: a fixed chance per message, the same for "guys I got
concert tickets" and for two people three replies deep into a conversation with each
other. That is why the bot barged into private threads and why it sometimes said
nothing for an hour and then something pointless.

This scores the message instead. The chance the owner sets is still what decides how
talkative it is overall - it just moves the bar rather than being the whole decision,
and a message that scores badly enough is not joined at any setting. Everything here
is local: no model call, no tokens, nothing to pay for.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

# Things it actually has opinions about. Deliberately small: this is a nudge, not a
# topic classifier, and a long list would fire on everything.
_LIKES = re.compile(
    r"(گربه|گربه‌ها|بچه\s*گربه|خرگوش|سگ|حیوون|غذا|خوشمزه|گشنمه|شام|ناهار|پیتزا|شکلات"
    r"|کیوت|ناز|خوشگل|بازی|گیم|انیمه|فیلم|سریال|آهنگ|موزیک|کنسرت|سفر|خواب|عکس"
    r"|\bcat\b|\bkitten\b|\bbunny\b|\brabbit\b|\bdog\b|\bfood\b|\bhungry\b|\bpizza\b"
    r"|\bcute\b|\bpretty\b|\bgame\b|\banime\b|\bmovie\b|\bmusic\b|\bconcert\b|\btrip\b)",
    re.I,
)
# Somebody opening the floor rather than talking to one person.
_OPEN = re.compile(
    r"(بچه‌ها|کسی\s*(هست|میدونه|بلده)|نظرتون|به\s*نظرتون|کی\s*(میاد|هست)|جدی\?|واقعا\?"
    r"|\bguys\b|\banyone\b|\bwho\s+(wants|else)\b|\bthoughts\b|\breally\?)",
    re.I,
)
_EXCITED = re.compile(r"(!{2,}|؟{2,}|\?{2,}|خخخ|هههه|واااا|\blol\b|\bhaha)", re.I)
_QUESTION = re.compile(r"[?؟]")
# Answers, agreements and sign-offs: a conversation closing, not one opening.
_CLOSING = re.compile(
    r"^(آره|نه|اوکی|باشه|مرسی|ممنون|دمت\s*گرم|فعلا|بای|خدافظ|شب\s*بخیر|ok|okay|yes|no"
    r"|thanks|thx|bye|gn|good\s*night)\b",
    re.I,
)

# A reply between two other people is their conversation. This is the single biggest
# reason the bot used to look like it was interrupting.
THREAD_PENALTY = 0.45
# Two of its own messages in a row is a monologue.
CONSECUTIVE_PENALTY = 0.35
BASELINE = 0.35


@dataclass(frozen=True)
class Interest:
    score: float
    reason: str

    def __bool__(self) -> bool:
        return self.score > 0


def rate(
    text: str,
    *,
    has_media: bool = False,
    in_thread: bool = False,
    spoke_last: bool = False,
    notes: str = "",
) -> Interest:
    """How much this message invites an uninvited reply, in [0, 1]."""
    body = (text or "").strip()
    score = BASELINE
    reasons: list[str] = []

    if has_media:
        score += 0.3
        reasons.append("media")
    if _OPEN.search(body):
        score += 0.25
        reasons.append("open question")
    elif _QUESTION.search(body):
        score += 0.1
        reasons.append("a question")
    if _LIKES.search(body):
        score += 0.2
        reasons.append("something it likes")
    if _EXCITED.search(body):
        score += 0.1
        reasons.append("excitement")
    if notes and _shares_a_word(body, notes):
        score += 0.15
        reasons.append("a running thing here")

    words = len(body.split())
    if _CLOSING.match(body) and words <= 4:
        score -= 0.3
        reasons.append("a sign-off")
    elif words <= 2 and not has_media:
        score -= 0.2
        reasons.append("barely a message")

    if in_thread:
        score -= THREAD_PENALTY
        reasons.append("two other people talking")
    if spoke_last:
        score -= CONSECUTIVE_PENALTY
        reasons.append("it just spoke")

    score = max(0.0, min(1.0, score))
    return Interest(score, ", ".join(reasons) or "nothing in particular")


def worth_joining(interest: Interest, chance: float, *, jitter: bool = True) -> bool:
    """Turn a score and the chat's talkativeness into a yes or a no.

    `chance` is the setting the owner controls: at 0 it never joins, at 1 it joins
    anything that is not actively somebody else's conversation. In between, the score
    has to clear a bar that the setting lowers. The coin flip survives only as a small
    wobble, so the bot is not perfectly predictable.
    """
    if chance <= 0 or interest.score <= 0:
        return False
    bar = 1.0 - chance
    value = interest.score
    if jitter:
        value += random.uniform(-0.08, 0.08)  # noqa: S311 - flavour, not a secret
    return value >= bar


def _shares_a_word(body: str, notes: str) -> bool:
    """Cheap overlap test against the chat's own notes, on longer words only."""
    words = set(re.findall(r"\w{5,}", body.casefold()))
    if not words:
        return False
    return bool(words & set(re.findall(r"\w{5,}", notes.casefold())))
