"""What a prompt is made of, as something small enough to choose between.

A recipe is not a prompt. It is the handful of decisions the renderer needs on
top of the locked layers: which voice, which mood, how many examples, how much
of the media block, how often to remind. Everything a recipe cannot say is a
rule the bot follows whatever the recipe says - that split lives in
`persona.LOCKED` and `persona.MUTABLE`, and this module cannot widen it.

Recipes are frozen and their renders are deterministic: the same recipe against
the same chat produces the same bytes on every turn, which is what keeps the
provider-side prompt cache warm. The cache key is the recipe, not the turn.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import persona

# The names a stored recipe may use for each field. Anything else is a typo or a
# corrupted row, and both resolve to the factory value rather than to an error.
BASES = (persona.LAYERED, persona.COMPACT)
MEDIA_FULL = "full"
MEDIA_COMPACT = "compact"
MEDIA = (MEDIA_FULL, MEDIA_COMPACT)

# How many examples a recipe may ask for. Four is already most of the block; the
# ceiling exists so a recipe cannot ask for a hundred and get a prompt built out
# of nothing but examples.
EXAMPLE_COUNTS = (0, 1, 2, 4, persona.ALL_EXAMPLES)
MAX_EXAMPLES = persona.ALL_EXAMPLES


@dataclass(frozen=True)
class Recipe:
    """One way of building the static prompt.

    Every field has a factory value, so a recipe read back from a half-written
    row still renders something sane rather than raising.
    """

    name: str = "factory"
    base: str = persona.LAYERED
    voice: str = "factory"
    mood: str = persona.BRIGHT
    examples: int = persona.ALL_EXAMPLES
    media: str = MEDIA_FULL
    # 0 means "follow the configured setting", which is what happens today.
    remind_every: int = 0
    # A permutation of persona.MUTABLE, or empty for the order in the skeleton.
    order: tuple[str, ...] = ()

    @property
    def compact(self) -> bool:
        return self.base == persona.COMPACT

    def render(self, *, is_group: bool = True, locale: str = "en",
               heavy_lifting: bool = False) -> str:
        return persona.render(
            self, is_group=is_group, locale=locale, heavy_lifting=heavy_lifting
        )

    def sanitised(self) -> Recipe:
        """The same recipe with every field forced back inside what is allowed.

        Called on everything read from storage. A recipe is data, and data that
        has been through a database and a JSON round trip is not trusted to name
        a real voice or a real mood, let alone a sensible number of examples.
        """
        return replace(
            self,
            base=self.base if self.base in BASES else persona.LAYERED,
            voice=self.voice if self.voice in persona.VOICES else "factory",
            mood=self.mood if self.mood in persona.MOODS else persona.BRIGHT,
            examples=max(0, min(int(self.examples or 0), MAX_EXAMPLES)),
            media=self.media if self.media in MEDIA else MEDIA_FULL,
            remind_every=max(0, min(int(self.remind_every or 0), 100)),
            order=tuple(self.order) if sorted(self.order) == sorted(persona.MUTABLE) else (),
        )


# The two prompts the bot has always had, named. Day one is a no-op: these render
# byte for byte what `static_prompt` and `compact_prompt` return, and a test says
# so. They are also the control arm the brain is measured against, and the thing
# it falls back to, so neither can ever be removed from the pool.
FACTORY_LAYERED = Recipe(name="layered")
FACTORY_COMPACT = Recipe(
    name="compact", base=persona.COMPACT, examples=1, media=MEDIA_COMPACT
)

FACTORY = {r.name: r for r in (FACTORY_LAYERED, FACTORY_COMPACT)}


def factory_for(*, free_mode: bool) -> Recipe:
    """What runs when nothing has been learned yet, or when the brain is off.

    The one switch the bot has today, in one place: free models are small and
    drown in the layered prompt, so free mode takes the short one.
    """
    return FACTORY_COMPACT if free_mode else FACTORY_LAYERED
