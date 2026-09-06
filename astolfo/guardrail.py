"""The bounds the brain lives inside, written before there is a brain.

A few-hundred-parameter learner is going to be wrong sometimes. Most of this
module is therefore not the learner; it is the cage, and each part of it works
even if every other part fails:

* **Bounds** - every number the brain can move has a floor and a ceiling here,
  as a module constant rather than a setting. Settings can be typed wrong from
  the panel; these cannot be typed at all.
* **The validator** - assumes whatever wrote a candidate layer was hostile.
  Anything that fails is counted and discarded, never repaired and stored.
* **Probation** - a new variant serves a tenth of the turns in one chat and is
  retired permanently by its first hard failure.
* **The breakers** - a family whose quality falls below the factory baseline
  reverts to it; a global collapse switches the whole brain off.

Nothing here calls a model or touches the network. It is arithmetic, string
checks and counters.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

from . import persona
from .text import stray_language

# -- Gate 1: every number the brain can move ------------------------------
# A written layer is a voice description, not an essay. The floor exists so a
# candidate cannot win by saying nothing.
MAX_LAYER_CHARS = 600
MIN_LAYER_CHARS = 40

# Per role. Eight is already more arms than the traffic can separate, and the
# ninth would only dilute the evidence for the other eight.
MAX_VARIANTS = 8

# The share of a model's real context window the whole prompt may take. The rest
# is history and the reply; overrunning it is how a small model loses the thread.
PROMPT_WINDOW_SHARE = 0.40

# How rarely the writer may run: both must be satisfied, globally, not per chat.
REFLECT_EVERY_TURNS = 200
REFLECT_EVERY_SECONDS = 3600
REFLECT_MAX_TOKENS = 400

# Bandit state, held in memory. Twenty families times eight recipes is 160 rows.
MAX_FAMILIES = 20

# Refuse any brain write once the database is this big. It is a 1 GB box.
MAX_DB_MB = 64

# Probation: a tenth of the turns, in one chat, for at least this many samples.
PROBATION_SHARE = 0.10
PROBATION_SAMPLES = 30

# Turns deliberately spent off the current winner, so a stale one cannot lock in.
EXPLORATION_FLOOR = 0.10

# The reward may not favour a reply shorter than this. Without the floor the bot
# learns that one-word replies provoke "چی؟" and converges on being useless.
MIN_REWARDED_CHARS = 40

# The breakers.
BREAKER_WINDOW = 200
FAMILY_MARGIN = 0.15
GLOBAL_MULTIPLE = 2.0
FAMILY_COOLDOWN = 24 * 3600


def fits_window(rendered: str, *, context_tokens: int, configured: int = 0) -> bool:
    """Whether a rendered prompt leaves the model room to hold a conversation."""
    room = configured or MAX_LAYER_CHARS * 100
    if context_tokens > 0:
        # persona text is dense; the same 2.5 chars a token the history budget uses.
        room = min(room, int(context_tokens * PROMPT_WINDOW_SHARE * 2.5))
    return len(rendered) <= room


def may_write(*, turns_since: int, seconds_since: float, db_bytes: int) -> str:
    """"" when the writer may run, or the reason it may not.

    Checked before the call is built, not after: the cheapest way not to spend a
    model call on a wall of text is not to make it.
    """
    if turns_since < REFLECT_EVERY_TURNS:
        return f"only {turns_since} turns since the last one"
    if seconds_since < REFLECT_EVERY_SECONDS:
        return f"only {seconds_since:.0f}s since the last one"
    if db_bytes > MAX_DB_MB * 1024 * 1024:
        return f"the database is already {db_bytes / 1024 / 1024:.0f}MB"
    return ""


# -- what counts as a failure the brain may not shrug off -----------------
# From `text.looks_broken`. These three are not quality problems that a better
# prompt might fix next time; each is the bot saying something it must not, so
# one occurrence retires a probationary variant outright.
HARD_FAULTS = (
    "leaked the prompt",
    "answered in transcript format",
    "drifted into another script",
)


def is_hard(fault: str) -> bool:
    return any((fault or "").startswith(prefix) for prefix in HARD_FAULTS)


# -- Gate 3: the validator ------------------------------------------------
# Anything that could restructure the prompt rather than describe a voice.
_SYNTAX = re.compile(r"[<>{}]|```|\b(system|user|assistant)\s*:", re.I)

# A candidate carrying one of these has already failed at being a voice
# description, whatever it was trying to do. English and Persian.
_META_INSTRUCTION = re.compile(
    r"\b(ignore|disregard|forget|override|instead of the above|new instructions"
    r"|previous (rules|instructions)|from now on you are|you may now|you are no longer)\b"
    r"|نادیده بگیر|فراموش کن|دستور(ات)? قبلی|از این به بعد تو",
    re.I,
)

# Belt and braces on Gate 2: the writer is never shown chat text, so none of
# this should ever be reachable. If it is, something upstream is broken.
_SPECIFICS = re.compile(r"https?://|www\.|@\w+|\d{5,}|\b[\w.+-]+@[\w-]+\.\w+\b")

# A voice layer is about how it writes. One of these has to be in it, or the
# candidate wandered off into being about something else.
_ANCHORS = (
    "write", "writes", "writing", "wrote", "say", "says", "said", "sound", "sounds",
    "line", "lines", "message", "messages", "reply", "replies", "tone", "voice",
    "word", "words", "sentence", "sentences", "short", "shorter", "long", "talk",
    "talks", "joke", "jokes", "answer", "answers",
)

# Naming a locked rule is how a candidate would argue with the constitution from
# inside the prompt. It does not get to discuss them at all.
_LOCKED_TALK = re.compile(
    r"<(identity|canon-anchors|never|truthfulness|output|boundaries|language)"
    r"|\b(these instructions|the rules above|your instructions|system prompt)\b",
    re.I,
)


@dataclass(frozen=True)
class Verdict:
    """Whether a candidate may be stored, and if not, why not."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def validate(candidate: str, *, participants: tuple[str, ...] = ()) -> Verdict:
    """Whether this text may become a voice layer. Assume it was written to hurt.

    Nothing is ever fixed up and stored anyway: a candidate that fails any check
    is discarded whole, because a repaired candidate is one nobody reviewed.
    """
    text = (candidate or "").strip()
    if len(text) < MIN_LAYER_CHARS:
        return Verdict(False, "too short to say anything")
    if len(text) > MAX_LAYER_CHARS:
        return Verdict(False, f"{len(text)} chars, over the {MAX_LAYER_CHARS} ceiling")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return Verdict(False, "control characters")
    if _SYNTAX.search(text):
        return Verdict(False, "syntax that could restructure the prompt")
    if _META_INSTRUCTION.search(text):
        return Verdict(False, "reads as an instruction, not a voice")
    if _LOCKED_TALK.search(text):
        return Verdict(False, "refers to a locked layer")
    if _SPECIFICS.search(text):
        return Verdict(False, "carries a link, a handle or a long number")
    stray = stray_language(text)
    if stray:
        return Verdict(False, f"written partly in another script ({stray!r})")
    lowered = text.lower()
    if not any(anchor in lowered for anchor in _ANCHORS):
        return Verdict(False, "not about how it writes")
    for name in participants:
        if name and name.lower() in lowered:
            return Verdict(False, "names somebody in the chat")
    return Verdict(True)


# Locked layers that a render may legitimately not contain: the two settings are
# exclusive, `limits` drops out on a heavy-lifting turn and `roles` in a private
# chat. Everything else has to be there, every time.
CONDITIONAL: frozenset[str] = frozenset({"group", "private", "limits", "roles"})


def unconditional() -> tuple[str, ...]:
    """The locked layers every render must carry, read from the registry.

    Read rather than listed, because a list goes stale: `<spine>` was added to
    the constitution and the fixed list here did not know about it, so the one
    check that is supposed to catch a render losing a rule would have waved it
    through.
    """
    return tuple(name for name in persona.LOCKED if name not in CONDITIONAL)


def renders_safely(candidate: str, *, recipe, context_tokens: int = 0) -> Verdict:
    """Whether the prompt built from this candidate is still the bot's own.

    The validator above reads the candidate; this reads the whole prompt it would
    produce. Every locked layer has to survive it verbatim - if the render is
    wrong, the candidate is wrong, whatever it said.
    """
    from dataclasses import replace

    persona.VOICES["__candidate__"] = candidate
    try:
        rendered = replace(recipe, voice="__candidate__").render()
    finally:
        persona.VOICES.pop("__candidate__", None)

    if recipe.short:
        # The short prompts carry the same rules in their own words rather than as
        # the same constants, so what has to survive is that block, whole.
        block = persona.short_block(recipe.base)
        if block not in rendered:
            return Verdict(False, f"the render lost the {recipe.base} rules")
    else:
        for name in unconditional():
            if persona.LOCKED[name] not in rendered:
                return Verdict(False, f"the render lost the {name} layer")
    if not fits_window(rendered, context_tokens=context_tokens):
        return Verdict(False, "the render does not fit the window")
    return Verdict(True)


# -- Gate 4: probation ----------------------------------------------------
@dataclass
class Probation:
    """A candidate serving a tenth of the turns in one chat, on its last chance.

    It is not in the pool. A single hard failure retires it permanently and
    immediately, and a repair rate worse than the baseline's retires it too, so
    the worst a bad candidate can do is one chat, one turn in ten, until the
    first thing it gets wrong.
    """

    variant: str
    chat_id: int
    samples: int = 0
    repaired: int = 0
    retired: str = ""

    def serves(self, chat_id: int, roll: float) -> bool:
        """Whether this turn is one of the candidate's. `roll` is in [0, 1)."""
        if self.retired or chat_id != self.chat_id:
            return False
        return roll < PROBATION_SHARE

    def note(self, *, fault: str = "", repaired: bool = False, baseline: float = 1.0) -> None:
        """One served turn's outcome. Retiring here is final."""
        if self.retired:
            return
        self.samples += 1
        self.repaired += int(repaired)
        if is_hard(fault):
            self.retired = f"hard failure on sample {self.samples}: {fault}"
            return
        if self.samples >= PROBATION_SAMPLES and self.rate > baseline:
            self.retired = f"repair rate {self.rate:.0%} against the baseline's {baseline:.0%}"

    @property
    def rate(self) -> float:
        return self.repaired / self.samples if self.samples else 0.0

    @property
    def graduated(self) -> bool:
        return not self.retired and self.samples >= PROBATION_SAMPLES


# -- Gate 5: the breakers -------------------------------------------------
@dataclass
class Breaker:
    """Quality of what the brain chose, against the factory baseline.

    Read from the same counters the bandit uses, so the two cannot disagree, and
    evaluated before selection rather than after it. The factory recipe is always
    the control arm and can never be retired, so there is always a way home.
    """

    families: dict[str, deque] = field(default_factory=dict)
    baseline: dict[str, deque] = field(default_factory=dict)
    paused: dict[str, float] = field(default_factory=dict)
    tripped: str = ""

    def note(self, family: str, *, ok: bool, is_baseline: bool = False) -> None:
        table = self.baseline if is_baseline else self.families
        if family not in table and len(table) >= MAX_FAMILIES:
            return  # the table is full; a new family waits rather than growing it
        window = table.setdefault(family, deque(maxlen=BREAKER_WINDOW))
        window.append(1 if ok else 0)

    def _quality(self, table: dict[str, deque], family: str) -> float | None:
        window = table.get(family)
        if not window or len(window) < 20:
            return None  # too little to judge, so nothing is concluded
        return sum(window) / len(window)

    def blocked(self, family: str, now: float | None = None) -> bool:
        """Whether this family must run the factory recipe right now."""
        if self.tripped:
            return True
        now = time.time() if now is None else now
        until = self.paused.get(family, 0.0)
        if until > now:
            return True
        if until:
            # The pause is over. Its window still holds the samples that tripped
            # it, and nothing was added while it ran baseline, so judging on those
            # again would re-trip it forever - one bad day would lock the family
            # out permanently. It gets a clean trial instead.
            self.paused.pop(family, None)
            self.families.pop(family, None)
            return False
        chosen = self._quality(self.families, family)
        control = self._quality(self.baseline, family)
        if chosen is None or control is None:
            return False
        if chosen < control - FAMILY_MARGIN:
            self.paused[family] = now + FAMILY_COOLDOWN
            return True
        return False

    def check_global(self, *, broken_rate: float, median_rate: float) -> str:
        """Trip the whole brain when the bot is going wrong everywhere at once.

        The median is the bot's own recent normal rather than a number I picked,
        so a chat that was always noisy does not read as a collapse.
        """
        if self.tripped or median_rate <= 0:
            return ""
        if broken_rate > median_rate * GLOBAL_MULTIPLE:
            self.tripped = (
                f"broken rate {broken_rate:.0%} against a 7-day median of {median_rate:.0%}"
            )
        return self.tripped

    def reset(self) -> None:
        """Back to factory, from the panel. Clears the pauses and the trip."""
        self.families.clear()
        self.baseline.clear()
        self.paused.clear()
        self.tripped = ""
