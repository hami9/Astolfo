"""Per-chat state: short-term history, long-term notes, and disk persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .learning import LEARN_RULES, Style
from .llm import LLMClient, Usage
from .text import shorten
from .tuning import Reception

log = logging.getLogger(__name__)

MAX_NOTES_CHARS = 900
SUMMARY_BATCH = 8
MAX_PARTICIPANTS = 20

# One pasted wall of text used to fill the whole history budget on its own and
# push every other turn out of the window, which reads as the bot suddenly
# forgetting the conversation. The first part of a long message carries what it
# was about; the rest is what the person is asking about right now anyway.
MAX_TURN_CHARS = 500

# Fold turns into notes once this many have gone unfolded, rather than waiting
# for the window to fill: a chat that has said 79 things had no long-term memory
# at all, and by the time the eightieth arrived the oldest were already leaving.
SUMMARY_EVERY = 12

# Characters per token, deliberately pessimistic. English averages around four,
# Persian is closer to two because most of it is outside the byte-pair vocabulary,
# and guessing high here is what makes a prompt overflow.
CHARS_PER_TOKEN = 2.5
# Never trim history below this, however small the model: a couple of turns of
# context is the difference between a reply and a non sequitur.
MIN_HISTORY_CHARS = 1200


def _json(raw: object) -> object:
    """A stored counter blob, or nothing. A bad row costs the counters, not the chat."""
    if not raw:
        return None
    try:
        return json.loads(str(raw))
    except (ValueError, TypeError):
        log.warning("could not read stored reception counters, starting them over")
        return None


def history_budget(
    wanted: int, *, context_tokens: int, overhead_chars: int, reply_tokens: int
) -> int:
    """Characters of history that will actually fit, not the ones we would like.

    HISTORY_CHAR_BUDGET is one number for every model, and the models now range
    from 8k of context to a million. Sending 9000 characters of history to a
    small model pushes the persona out of the front of the window, which reads
    exactly like the bot losing the thread mid-conversation.
    """
    if context_tokens <= 0:
        return wanted  # nothing known about this model; trust the setting
    room = int(context_tokens * CHARS_PER_TOKEN) - overhead_chars - int(reply_tokens * 4)
    return max(MIN_HISTORY_CHARS, min(wanted, room))


@dataclass
class ChatState:
    chat_id: int
    history: deque[dict]
    notes: str = ""
    style: Style = field(default_factory=Style)
    # Which reply lengths people here actually answer.
    reception: Reception = field(default_factory=Reception)
    # True while the bot spoke last and nobody has answered it yet.
    awaiting_reply: bool = False
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
    mode: str = ""  # manual | auto | smart, empty means follow the global one
    daily_limit: int = 0  # 0 means follow the global one
    # Dormant: not muted but switched off. Nothing is read, stored or answered.
    off: bool = False
    # How many turns have already been folded into notes, counted against
    # turn_count, so the same messages are not summarised again and again.
    folded_turns: int = 0
    # Messages waiting for this chat's lock, so a flood cannot queue without end.
    waiting: int = 0
    # Just the arrival times, for telling a busy chat from a quiet one.
    seen_at: deque[float] = field(default_factory=lambda: deque(maxlen=60))

    def touch_participant(self, name: str) -> None:
        self.participants[name] = time.time()
        self.participants.move_to_end(name)
        while len(self.participants) > MAX_PARTICIPANTS:
            self.participants.popitem(last=False)

    def add_user(self, name: str, text: str, *, answering: str = "") -> None:
        # "Sara → Reza: ..." costs three tokens and is the whole difference
        # between one conversation and two happening in the same window.
        who = f"{name} → {answering}" if answering and answering != name else name
        self.history.append({"role": "user", "content": f"{who}: {text}"})
        self.turn_count += 1
        self.seen_at.append(time.monotonic())
        self.touch_participant(name)

    def pace(self) -> float:
        """Messages a minute lately, from the arrival times alone."""
        if len(self.seen_at) < 2:
            return 0.0
        span = self.seen_at[-1] - self.seen_at[0]
        return len(self.seen_at) / (span / 60) if span > 1 else float(len(self.seen_at))

    def add_assistant(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})
        self.turn_count += 1
        self.replies_sent += 1
        self.reception.note_sent(len(text or ""))
        self.awaiting_reply = True

    def note_reception(self, *, answered: bool) -> None:
        """Whether the last thing it said got an answer. Counted once."""
        if not self.awaiting_reply:
            return
        self.awaiting_reply = False
        if answered:
            self.reception.note_answered()
        else:
            self.reception.note_ignored()

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
            if len(content) > MAX_TURN_CHARS:
                content = shorten(content, MAX_TURN_CHARS)
                turn = {**turn, "content": content}
            cost = len(content) + 8
            if used + cost > char_budget and selected:
                break
            selected.append(turn)
            used += cost
        selected.reverse()
        return merge_runs(selected)


# More than this many messages already waiting for one chat and the rest are
# dropped: a flood should not queue up replies nobody is waiting for any more.
MAX_WAITING = 2


@asynccontextmanager
async def composing(state: ChatState):
    """Hold the chat's lock, counting who is queued behind it.

    A second message used to be dropped outright while a reply was being
    composed, so being spoken to during someone else's turn got no answer at
    all. Waiting is bounded, because a burst of fifty messages should not
    become fifty replies.
    """
    state.waiting += 1
    try:
        async with state.lock:
            yield
    finally:
        state.waiting -= 1


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

    def __init__(self, settings: Settings, database: Database) -> None:
        self._s = settings
        self.db = database
        self._chats: OrderedDict[int, ChatState] = OrderedDict()
        self._legacy_path = os.path.join(settings.data_dir, "state.json")
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

    def configure(self, settings: Settings) -> None:
        self._s = settings

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
        self._import_legacy_file()
        for row in self.db.chat_settings():
            chat_id = int(row["chat_id"])
            self._chats[chat_id] = ChatState(
                chat_id=chat_id,
                history=deque(maxlen=self._s.max_history),
                notes=str(row["notes"] or "")[:MAX_NOTES_CHARS],
                reply_chance=row["reply_chance"],
                forced_mode=row["forced_mode"],
                muted=bool(row["muted"]),
                title=str(row["title"] or ""),
                locale=row["locale"],
                mode=str(row["mode"] or ""),
                daily_limit=int(row["daily_limit"] or 0),
                off=bool(row["dormant"]),
                style=Style.loads(row["style"]),
                reception=Reception.load(_json(row["reception"])),
            )
        if self._chats:
            log.info("restored settings for %d chats", len(self._chats))

    def _import_legacy_file(self) -> None:
        """Carry a pre-database install over, once, then leave the file alone."""
        try:
            with open(self._legacy_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("could not read the old state file: %s", exc)
            return

        imported = 0
        for raw_id, blob in (data.get("chats") or {}).items():
            try:
                chat_id = int(raw_id)
            except ValueError:
                continue
            if self.db.chat(chat_id):
                continue
            self.db.save_chat_state(
                chat_id,
                notes=str(blob.get("notes") or "")[:MAX_NOTES_CHARS],
                reply_chance=blob.get("reply_chance"),
                forced_mode=blob.get("forced_mode"),
                muted=1 if blob.get("muted") else 0,
                title=str(blob.get("title") or ""),
                locale=blob.get("locale"),
            )
            imported += 1
        if imported:
            log.info("imported %d chats from the old state file into the database", imported)

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        for state in self._chats.values():
            self.db.save_chat_state(
                state.chat_id,
                notes=state.notes,
                reply_chance=state.reply_chance,
                forced_mode=state.forced_mode,
                muted=1 if state.muted else 0,
                title=state.title,
                locale=state.locale,
                mode=state.mode,
                daily_limit=state.daily_limit,
                dormant=1 if state.off else 0,
                style=state.style.dumps(),
                reception=json.dumps(state.reception.as_dict()) if state.reception.sent else "",
            )
        self._dirty = False


# One call does both jobs. Learning how a chat likes to be talked to is worth
# very little if it costs a second request per fold, and on the free tier the
# ration is counted in requests rather than tokens.
SUMMARY_PROMPT = f"""\
You maintain the long-term memory notes of a persona bot inside a Telegram chat.
Merge OLD NOTES with NEW MESSAGES into updated notes.

Keep only what helps a member of this chat sound like they belong later: who the
regulars are and what they are into, running jokes, ongoing situations, stated
preferences, plans, and anything the bot promised. Drop small talk and greetings.

Rules: write in the chat's own language, plain short lines, at most 8 lines and 700
characters, no markdown, and never invent anything that was not actually said. If
nothing is worth remembering, return the old notes unchanged.

{LEARN_RULES}

Reply with JSON only, and nothing else:
{{"notes": "...", "style": "...", "people": {{"name": "..."}}}}"""


async def update_notes(llm: LLMClient, settings: Settings, state: ChatState) -> Usage:
    """Fold the oldest turns into long-term notes. Runs in the background."""
    if not settings.summaries or state.summarizing:
        return Usage()
    if state.turn_count - state.folded_turns < SUMMARY_EVERY:
        return Usage()

    state.summarizing = True
    try:
        turns = list(state.history)
        # The window holds the newest turns, so the oldest one in it is this far
        # along in the chat's whole life. Anything before folded_turns is done.
        oldest = state.turn_count - len(turns)
        start = max(0, state.folded_turns - oldest)
        batch = [
            turn["content"]
            for turn in turns[start : start + SUMMARY_BATCH]
            if isinstance(turn.get("content"), str)
        ]
        if not batch:
            # Everything unfolded fell out of the window before we got to it.
            state.folded_turns = state.turn_count
            return Usage()
        state.folded_turns = min(state.turn_count, oldest + start + SUMMARY_BATCH)

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
        if not data:
            return usage
        if isinstance(data.get("notes"), str):
            notes = data["notes"].strip()[:MAX_NOTES_CHARS]
            if notes:
                state.notes = notes
        state.style.learn(
            chat=data.get("style") if isinstance(data.get("style"), str) else "",
            people=data.get("people") if isinstance(data.get("people"), dict) else {},
        )
        return usage
    except Exception as exc:
        log.warning("summary failed for chat %s: %s", state.chat_id, exc)
        return Usage()
    finally:
        state.summarizing = False
