"""What the bot has worked out about how to talk here, and to each person.

The long-term notes in `memory` are about *what* was said. This is about *how*:
that one group answers in Finglish and hates long messages, that one person only
ever jokes and another asks real questions. A persona bot that never adapts reads
as a script; one that adapts per group and per person reads as a regular.

It costs nothing extra to run. The lines are folded out of the summary call that
already happens every dozen turns, and only the lines about whoever is in the
current turn are ever sent, so a group of twenty people still pays for two.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MAX_CHAT_STYLE = 200
MAX_PERSON_STYLE = 100
# Remembering everyone would grow without end on a 1 GB box, and the people who
# have not spoken in weeks are not the ones the next reply is aimed at.
MAX_PEOPLE = 12


def _key(name: str) -> str:
    return " ".join((name or "").split()).casefold()[:32]


@dataclass
class Style:
    """One short line for the chat, and one for each person in it."""

    chat: str = ""
    people: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.chat or self.people)

    def note_for(self, name: str) -> str:
        return self.people.get(_key(name), "")

    def for_turn(self, *names: str) -> str:
        """Only what applies to this turn. Everything else is dead weight."""
        lines = [f"this chat: {self.chat}"] if self.chat else []
        seen: set[str] = set()
        for name in names:
            key = _key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            note = self.people.get(key)
            if note:
                lines.append(f"{name}: {note}")
        return "\n".join(lines)

    def learn(self, *, chat: str = "", people: dict | None = None) -> bool:
        """Merge one round of observations. True when anything actually changed."""
        changed = False
        line = " ".join(str(chat or "").split())[:MAX_CHAT_STYLE]
        if line and line != self.chat:
            self.chat = line
            changed = True

        for raw_name, raw_note in (people or {}).items():
            key = _key(str(raw_name))
            note = " ".join(str(raw_note or "").split())[:MAX_PERSON_STYLE]
            if not key or not note:
                continue
            if self.people.get(key) != note:
                changed = True
            # Reinserted either way, so the newest observation is also the
            # youngest entry and survives the trim below.
            self.people.pop(key, None)
            self.people[key] = note

        while len(self.people) > MAX_PEOPLE:
            self.people.pop(next(iter(self.people)))
            changed = True
        return changed

    def forget(self) -> None:
        self.chat = ""
        self.people.clear()

    def summary(self) -> str:
        """How it reads on the panel."""
        if not self:
            return "nothing learned yet"
        parts = [self.chat] if self.chat else []
        parts += [f"{name}: {note}" for name, note in self.people.items()]
        return "\n".join(parts)

    def dumps(self) -> str:
        if not self:
            return ""
        return json.dumps({"chat": self.chat, "people": self.people}, ensure_ascii=False)

    @classmethod
    def loads(cls, raw: str | None) -> Style:
        """Never raise on a stored value: a bad row costs the style, not the chat."""
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("could not read a stored style, starting it over")
            return cls()
        if not isinstance(data, dict):
            return cls()
        style = cls()
        style.learn(
            chat=str(data.get("chat") or ""),
            people=data.get("people") if isinstance(data.get("people"), dict) else {},
        )
        return style


LEARN_RULES = """\
Also report how this chat likes to be talked to, so the bot fits in better next time.
"style" is one short line about the chat as a whole: which language and register they
use, how long a message they tolerate, the humour, and what falls flat.
"people" is at most four entries, name to one short line, only for someone whose way
of talking you actually saw: how they write, what they come here for, how they like
being answered. Never guess at anyone's identity, job or personal life, and never
record anything sensitive about a person.
Both are about manner, never about facts, and both may be empty when nothing new
showed up. Keep every line under 100 characters and in the chat's own language."""
