"""Which prompt to use for the model that is about to answer.

The bot's prompt does not know which model it is talking to. One switch chooses
between a long prompt and a short one, and every model on either side of it gets
byte-identical text. Prompt sensitivity is relative to the model, though: the
worst prompt for a 550B model is not the worst prompt for a 7B one, and the free
pool changes what is running from week to week.

So the choice is learned instead of configured. Thompson sampling over a handful
of recipes, keyed by model family rather than by model id, because a family is
what survives a rename - `command-r-08-2024` and `command-r-03-2024` are the same
thing to a prompt.

What this module is not allowed to do is the point of the module next to it.
Every recipe it can choose renders the same locked layers; the bandit only ever
moves the mutable ones. It reads counters, samples a distribution and returns a
recipe. It never calls a model, never touches the database directly, and asks
`guardrail.Breaker` before it is allowed to choose at all.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field, replace

from . import recipes
from .guardrail import EXPLORATION_FLOOR, MAX_FAMILIES, MAX_VARIANTS, MIN_REWARDED_CHARS, Breaker

log = logging.getLogger(__name__)

# How much evidence a family needs before the brain is allowed to prefer anything.
# Under this it returns the factory recipe, which is what the bot does today.
ENOUGH = 30

# The reward, entirely from counters the bot already keeps.
ANSWERED = 1.0
REPAIRED = -0.5
BROKEN = -2.0
# Multiplied by the share of the token ceiling the reply used, so a recipe that
# wins by writing twice as much does not win.
TOKEN_WEIGHT = -0.3

FLOOR = BROKEN + TOKEN_WEIGHT
CEILING = ANSWERED

# Families worth telling apart, longest match first. Everything else falls back
# to the shape of the id.
_KNOWN = (
    "command-r7b", "command-r", "command",
    "llama-4", "llama-3.3", "llama-3.2", "llama-3.1", "llama-3", "llama",
    "qwen3", "qwen2.5", "qwen2", "qwen",
    "gemini", "gemma-3", "gemma-2", "gemma",
    "deepseek", "mistral", "ministral", "pixtral", "nemotron", "magistral",
    "glm", "minimax", "inkling", "kimi", "hermes", "olmo", "grok", "phi", "smollm",
    "gpt-oss", "gpt-5", "gpt-4", "o4-mini", "claude", "sonar",
)

# Words that mean a different model rather than a different release. Appended to
# the family so `gemini-2.5-flash` and `gemini-2.5-pro` are not one arm.
_LINES = ("flash", "pro", "mini", "coder", "scout", "maverick")

# A dated release: -08-2024, -20250514, -v0.1. None of it changes the prompt.
_RELEASE = re.compile(r"[-_](?:\d{2}-\d{4}|\d{4}-\d{2}(?:-\d{2})?|\d{6,8}|v?\d+(?:\.\d+)*)$")


def family(model: str) -> str:
    """The name to learn under, so a rename inherits what the last one taught."""
    lowered = (model or "").split("/", 1)[-1].lower()
    lowered = lowered.split(":", 1)[0].strip()
    if not lowered:
        return "unknown"

    best = ""
    for name in _KNOWN:
        if name in lowered and len(name) > len(best):
            best = name
    if not best:
        # Nothing recognised. Drop the release suffix and keep the first two
        # words, which is what an id is usually built out of.
        stripped = _RELEASE.sub("", lowered)
        best = "-".join(stripped.split("-")[:2]) or lowered

    for line in _LINES:
        if re.search(rf"[-_]{line}\b", lowered) and line not in best:
            return f"{best}-{line}"
    return best


def reward(
    *,
    answered: bool = False,
    chars: int = 0,
    repaired: bool = False,
    broken: bool = False,
    tokens: int = 0,
    ceiling: int = 0,
) -> float:
    """What one turn was worth, in [FLOOR, CEILING].

    The length floor on the positive term is deliberate. Without it the bot
    learns that a one-word reply provokes "چی؟", counts that as somebody
    answering, and converges on being useless.
    """
    value = 0.0
    if answered and chars >= MIN_REWARDED_CHARS and not repaired and not broken:
        value += ANSWERED
    if repaired:
        value += REPAIRED
    if broken:
        value += BROKEN
    if ceiling > 0 and tokens > 0:
        value += TOKEN_WEIGHT * min(1.0, tokens / ceiling)
    return max(FLOOR, min(CEILING, value))


def _scaled(value: float) -> float:
    """The reward as a number between 0 and 1, which is what a Beta arm takes."""
    return (value - FLOOR) / (CEILING - FLOOR)


def pool(*, free_mode: bool) -> tuple[recipes.Recipe, ...]:
    """What there is to choose between before anything has been written.

    The three weights first, because that is the heaviest lever there is: the
    full prompt is fifteen times the tight one, and which of them a model can
    actually hold is a fact about that model rather than about the bot. Then a
    couple of example counts, which is a much finer adjustment.

    The recipe the bot would have used anyway is always first and can never
    leave, so the control arm is always available and there is always a way home.
    Every one of these renders the same locked layers; only mutable fields move.
    """
    here = recipes.factory_for(free_mode=free_mode)
    out: list[recipes.Recipe] = [here]
    for weight in recipes.FACTORY.values():
        if weight.name != here.name and len(out) < MAX_VARIANTS:
            out.append(weight)
    for count in (1, 2):
        if len(out) >= MAX_VARIANTS or count == here.examples:
            continue
        out.append(replace(here, name=f"{here.name}+{count}ex", examples=count))
    return tuple(out)


@dataclass
class Arm:
    """One recipe's record for one family, as a Beta posterior.

    Rewards are bounded rather than binary, so each turn adds a fraction of a win
    and the rest of a loss. Two counts and a sample; there is no model here.
    """

    wins: float = 0.0
    losses: float = 0.0
    samples: int = 0

    def note(self, value: float) -> None:
        share = _scaled(value)
        self.wins += share
        self.losses += 1.0 - share
        self.samples += 1

    def draw(self, rng: random.Random) -> float:
        return rng.betavariate(1.0 + self.wins, 1.0 + self.losses)

    @property
    def mean(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.5


@dataclass
class Brain:
    """The bandit, off by default.

    `on` is the panel switch. With it off, `choose` returns exactly what the bot
    returns today, and a test asserts the prompt is byte-identical - a bug in
    here cannot reach the chat until somebody turns it on.
    """

    on: bool = False
    arms: dict[tuple[str, str], Arm] = field(default_factory=dict)
    breaker: Breaker = field(default_factory=Breaker)
    rng: random.Random = field(default_factory=random.Random)
    # Set by `note`, cleared by whoever writes the counters out. Nothing here
    # writes per turn; the existing autosave carries it.
    dirty: bool = False

    def _arm(self, fam: str, name: str) -> Arm:
        key = (fam, name)
        arm = self.arms.get(key)
        if arm is None:
            if len({f for f, _ in self.arms} | {fam}) > MAX_FAMILIES:
                return Arm()  # the table is full: this family learns nothing
            arm = self.arms[key] = Arm()
        return arm

    def seen(self, fam: str) -> int:
        return sum(arm.samples for (f, _), arm in self.arms.items() if f == fam)

    def choose(self, *, model: str, free_mode: bool) -> recipes.Recipe:
        """The recipe for this turn. Never blocks, never raises, always returns."""
        factory = recipes.factory_for(free_mode=free_mode)
        if not self.on:
            return factory
        fam = family(model)
        # Before selection, not after: a family the breaker has paused runs the
        # control arm and nothing else, whatever the counters say.
        if self.breaker.blocked(fam):
            return factory
        if self.seen(fam) < ENOUGH:
            return factory

        options = pool(free_mode=free_mode)
        if self.rng.random() < EXPLORATION_FLOOR:
            # Deliberately off the winner, so a recipe that was best in a week
            # that no longer exists cannot hold the family forever.
            return self.rng.choice(options)
        return max(options, key=lambda r: self._arm(fam, r.name).draw(self.rng))

    def note(
        self,
        *,
        model: str,
        recipe: recipes.Recipe,
        free_mode: bool,
        answered: bool = False,
        chars: int = 0,
        repaired: bool = False,
        broken: bool = False,
        tokens: int = 0,
        ceiling: int = 0,
    ) -> float:
        """One turn's outcome, into both the bandit and the breaker.

        Recorded whether or not the brain is on: with it off this is the control
        arm building the baseline the breaker needs, which is why turning the
        switch on is not starting from nothing.
        """
        value = reward(
            answered=answered,
            chars=chars,
            repaired=repaired,
            broken=broken,
            tokens=tokens,
            ceiling=ceiling,
        )
        fam = family(model)
        self._arm(fam, recipe.name).note(value)
        self.breaker.note(
            fam,
            ok=not broken and not repaired,
            is_baseline=recipe.name == recipes.factory_for(free_mode=free_mode).name,
        )
        self.dirty = True
        return value

    # -- what the panel and the autosave read ------------------------------
    def rows(self) -> list[dict]:
        """The counters, flat enough to store and small enough to keep in memory."""
        return [
            {
                "family": fam,
                "recipe": name,
                "wins": round(arm.wins, 4),
                "losses": round(arm.losses, 4),
                "samples": arm.samples,
            }
            for (fam, name), arm in sorted(self.arms.items())
        ]

    def restore(self, rows: list[dict] | None) -> None:
        """Carry the counters into this run, refusing anything malformed."""
        self.arms.clear()
        for row in rows or []:
            try:
                fam = str(row["family"])[:60]
                name = str(row["recipe"])[:60]
                arm = Arm(
                    wins=max(0.0, float(row.get("wins") or 0.0)),
                    losses=max(0.0, float(row.get("losses") or 0.0)),
                    samples=max(0, int(row.get("samples") or 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if len({f for f, _ in self.arms} | {fam}) > MAX_FAMILIES:
                continue
            self.arms[(fam, name)] = arm
        if self.arms:
            log.info("brain restored %d arm(s) across %d families",
                     len(self.arms), len({f for f, _ in self.arms}))
