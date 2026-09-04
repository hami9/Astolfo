"""How long the next reply is allowed to be, decided from evidence.

Two things are measured, both for free, from what the bot already does:

*Cost.* Tokens in, tokens out and money spent are already recorded per call. When a
chat is expensive, or the day's budget is running down, the ceiling comes down with
it - a shorter answer is the cheapest saving there is, and on the free tier it is
also the fastest.

*Whether anybody cared.* After the bot speaks, somebody either answers it or does
not. That is a real signal and it costs nothing to collect. Replies are bucketed by
length; the bucket people answer most often is the one it aims for. A group that
ignores long messages gets short ones, and a group that talks back to them keeps
them.

Nothing here calls a model. It is arithmetic over counters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Reply length buckets, in characters. Roughly: one line, two lines, longer.
SHORT = "short"
MEDIUM = "medium"
LONG = "long"
BUCKETS = (SHORT, MEDIUM, LONG)
BOUNDS = {SHORT: 120, MEDIUM: 280}
# Target characters for each bucket, which is what the token ceiling is derived from.
TARGET = {SHORT: 110, MEDIUM: 240, LONG: 420}

# Below this many samples a bucket's rate is noise, so the default stands.
ENOUGH = 8
# A bucket has to beat the next one by this much to win, so a single unanswered
# message out of eight does not move the target and the ceiling does not flap.
MARGIN = 0.2

# Once this much of the daily budget is gone, replies get shorter.
TIGHT = 0.6
# Characters per token, matching memory.CHARS_PER_TOKEN.
CHARS_PER_TOKEN = 2.5
MIN_TOKENS = 60


def bucket(length: int) -> str:
    if length <= BOUNDS[SHORT]:
        return SHORT
    if length <= BOUNDS[MEDIUM]:
        return MEDIUM
    return LONG


@dataclass
class Reception:
    """Did people answer the bot, per length of what it said?

    Two counters per bucket: how often it sent one, and how often a human replied to
    it or spoke to it right afterwards. Message text is never part of this.
    """

    sent: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BUCKETS, 0))
    answered: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BUCKETS, 0))
    # The bucket of the reply still waiting to see whether anyone answers it.
    pending: str = ""

    def note_sent(self, length: int) -> None:
        name = bucket(length)
        self.sent[name] = self.sent.get(name, 0) + 1
        self.pending = name

    def note_answered(self) -> None:
        """Somebody replied to the bot, so the reply it is waiting on landed."""
        if not self.pending:
            return
        self.answered[self.pending] = self.answered.get(self.pending, 0) + 1
        self.pending = ""

    def note_ignored(self) -> None:
        """The conversation moved on without it. Counted only once."""
        self.pending = ""

    def rate(self, name: str) -> float:
        sent = self.sent.get(name, 0)
        return self.answered.get(name, 0) / sent if sent else 0.0

    def best(self) -> str:
        """The length people answer most, or "" while there is not enough to go on.

        Empty is the important case: with no evidence the configured ceiling stands
        untouched, so the setting means what it says until the chat says otherwise.
        """
        ranked = [
            (self.rate(name), name) for name in BUCKETS if self.sent.get(name, 0) >= ENOUGH
        ]
        if len(ranked) < 2:
            return ""  # nothing to compare it against yet
        top_rate, top = max(ranked)
        runner_up = max(rate for rate, name in ranked if name != top)
        return top if top_rate - runner_up >= MARGIN else ""

    def summary(self) -> str:
        parts = [
            f"{name} {self.answered.get(name, 0)}/{self.sent.get(name, 0)}"
            for name in BUCKETS
            if self.sent.get(name, 0)
        ]
        return ", ".join(parts) or "nothing measured yet"

    def as_dict(self) -> dict:
        if not any(self.sent.values()):
            return {}
        return {"sent": self.sent, "answered": self.answered}

    @classmethod
    def load(cls, data: object) -> Reception:
        if not isinstance(data, dict):
            return cls()
        got = cls()
        for field_name in ("sent", "answered"):
            values = data.get(field_name)
            if not isinstance(values, dict):
                continue
            target = getattr(got, field_name)
            for name in BUCKETS:
                try:
                    target[name] = max(0, int(values.get(name, 0)))
                except (TypeError, ValueError):
                    target[name] = 0
        return got


def reply_ceiling(rt, state, *, base: int, mode_is_fast: bool = True) -> int:
    """Tokens the next reply may use, from the budget and from what lands here."""
    ceiling = base
    lands = state.reception.best()
    if mode_is_fast and lands:
        # Only banter is tuned by length, and only once this chat has actually shown
        # a preference. A think or search answer needs its room either way.
        ceiling = min(ceiling, int(TARGET[lands] / CHARS_PER_TOKEN) + 40)

    budget = rt.settings.daily_budget_usd
    if budget > 0:
        spent = rt.budget.today_cost() / budget
        if spent >= TIGHT:
            # Straight-line from full length at the threshold to half at the cap.
            over = min(1.0, (spent - TIGHT) / max(0.01, 1.0 - TIGHT))
            ceiling = int(ceiling * (1.0 - 0.5 * over))
    return max(MIN_TOKENS, ceiling)


def brevity_hint(state) -> str:
    """One line for the prompt when this chat has shown it wants it shorter."""
    if state.reception.best() != SHORT:
        return ""
    return (
        "People here answer your short messages and scroll past your long ones. "
        "One line, and stop when the thought is finished."
    )
