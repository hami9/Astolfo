"""Per-chat state: short-term history, long-term notes, and disk persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from .config import Settings
from .llm import LLMClient, Usage

log = logging.getLogger(__name__)

MAX_NOTES_CHARS = 900
SUMMARY_BATCH = 8
MAX_PARTICIPANTS = 20


@dataclass
class ChatState:
    chat_id: int
    history: deque[dict]
    notes: str = ""
    participants: OrderedDict[str, float] = field(default_factory=OrderedDict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # -inf, not 0.0: time.monotonic() starts near zero on a freshly booted host,
    # which would make "never happened" look recent.
    last_reply_at: float = float("-inf")
    last_seen: float = field(default_factory=time.monotonic)
    budget_notice_at: float = float("-inf")
    error_notice_at: float = float("-inf")
    reply_chance: float | None = None
    forced_mode: str | None = None
    muted: bool = False
    turn_count: int = 0
    replies_sent: int = 0
    summarizing: bool = False
    title: str = ""
    locale: str | None = None

    def touch_participant(self, name: str) -> None:
        self.participants[name] = time.time()
        self.participants.move_to_end(name)
        while len(self.participants) > MAX_PARTICIPANTS:
            self.participants.popitem(last=False)

    def add_user(self, name: str, text: str) -> None:
        self.history.append({"role": "user", "content": f"{name}: {text}"})
        self.turn_count += 1
        self.touch_participant(name)

    def add_assistant(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})
        self.turn_count += 1
        self.replies_sent += 1

    def recent_texts(self, count: int = 6) -> list[str]:
        return [
            turn["content"]
            for turn in list(self.history)[-count:]
            if isinstance(turn.get("content"), str)
        ]

    def prompt_history(self, char_budget: int, *, skip_last: bool = True) -> list[dict]:
        """Newest turns that fit the character budget, in chronological order.

        Trimming by size instead of a fixed count keeps input cost predictable when
        people paste long messages.
        """
        turns = list(self.history)
        if skip_last and turns:
            turns = turns[:-1]

        selected: list[dict] = []
        used = 0
        for turn in reversed(turns):
            content = turn.get("content")
            if not isinstance(content, str):
                continue
            cost = len(content) + 8
            if used + cost > char_budget and selected:
                break
            selected.append(turn)
            used += cost
        selected.reverse()
        return merge_runs(selected)


def merge_runs(turns: list[dict]) -> list[dict]:
    """Collapse consecutive same-role turns into one message.

    In a group the bot stays quiet most of the time, so unanswered messages pile
    up as a run of separate `user` turns. A model reads that as a queue of
    questions and answers every one of them in a single reply; merging the run
    presents it as what it is, a stretch of conversation it overheard.
    """
    merged: list[dict] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] = f"{merged[-1]['content']}\n{turn['content']}"
        else:
            merged.append(dict(turn))
    return merged


class ChatStore:
    """TTL + LRU store. Only settings and notes are persisted, never message text."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._chats: OrderedDict[int, ChatState] = OrderedDict()
        self._path = os.path.join(settings.data_dir, "state.json")
        self._dirty = False
        self._load()

    def get(self, chat_id: int) -> ChatState:
        state = self._chats.get(chat_id)
        if state is None:
            state = ChatState(chat_id=chat_id, history=deque(maxlen=self._s.max_history))
            self._chats[chat_id] = state
        state.last_seen = time.monotonic()
        self._chats.move_to_end(chat_id)
        self._evict()
        return state

    def all_states(self) -> list[ChatState]:
        return list(self._chats.values())

    def mark_dirty(self) -> None:
        self._dirty = True

    def _evict(self) -> None:
        now = time.monotonic()
        for chat_id in [
            cid
            for cid, state in self._chats.items()
            if now - state.last_seen > self._s.chat_ttl and not state.lock.locked()
        ]:
            self._chats.pop(chat_id, None)

        while len(self._chats) > self._s.max_chats:
            chat_id, state = self._chats.popitem(last=False)
            if state.lock.locked():  # never drop a chat mid-reply
                self._chats[chat_id] = state
                self._chats.move_to_end(chat_id)
                break

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("could not read persisted state: %s", exc)
            return

        for raw_id, blob in (data.get("chats") or {}).items():
            try:
                chat_id = int(raw_id)
            except ValueError:
                continue
            self._chats[chat_id] = ChatState(
                chat_id=chat_id,
                history=deque(maxlen=self._s.max_history),
                notes=str(blob.get("notes") or "")[:MAX_NOTES_CHARS],
                reply_chance=blob.get("reply_chance"),
                forced_mode=blob.get("forced_mode"),
                muted=bool(blob.get("muted")),
                title=str(blob.get("title") or ""),
                locale=blob.get("locale"),
            )
        log.info("restored settings for %d chats", len(self._chats))

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        payload = {
            "chats": {
                str(cid): {
                    "notes": state.notes,
                    "reply_chance": state.reply_chance,
                    "forced_mode": state.forced_mode,
                    "muted": state.muted,
                    "title": state.title,
                    "locale": state.locale,
                }
                for cid, state in self._chats.items()
                if state.notes
                or state.reply_chance is not None
                or state.forced_mode
                or state.muted
            }
        }
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception as exc:
            log.warning("could not persist state: %s", exc)


SUMMARY_PROMPT = """\
You maintain the long-term memory notes of a persona bot inside a Telegram chat.
Merge OLD NOTES with NEW MESSAGES into updated notes.

Keep only what helps a member of this chat sound like they belong later: who the
regulars are and what they are into, running jokes, ongoing situations, stated
preferences, plans, and anything the bot promised. Drop small talk and greetings.

Rules: write in the chat's own language, plain short lines, at most 8 lines and 700
characters, no markdown, and never invent anything that was not actually said. If
nothing is worth remembering, return the old notes unchanged.

Reply with JSON only: {"notes": "..."}"""


async def update_notes(llm: LLMClient, settings: Settings, state: ChatState) -> Usage:
    """Fold the oldest turns into long-term notes. Runs in the background."""
    if not settings.summaries or state.summarizing:
        return Usage()
    if len(state.history) < (state.history.maxlen or 0):
        return Usage()

    state.summarizing = True
    try:
        batch = [
            turn["content"]
            for turn in list(state.history)[:SUMMARY_BATCH]
            if isinstance(turn.get("content"), str)
        ]
        if not batch:
            return Usage()

        data, usage = await llm.json_chat(
            [
                {"role": "system", "content": SUMMARY_PROMPT},
                {
                    "role": "user",
                    "content": f"OLD NOTES:\n{state.notes or '(empty)'}\n\nNEW MESSAGES:\n"
                    + "\n".join(batch),
                },
            ],
            model=settings.model_summary,
            max_tokens=400,
        )
        if data and isinstance(data.get("notes"), str):
            notes = data["notes"].strip()[:MAX_NOTES_CHARS]
            if notes:
                state.notes = notes
        return usage
    except Exception as exc:
        log.warning("summary failed for chat %s: %s", state.chat_id, exc)
        return Usage()
    finally:
        state.summarizing = False
