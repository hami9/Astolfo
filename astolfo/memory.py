"""حافظهٔ گفت‌وگو: تاریخچهٔ کوتاه‌مدت + یادداشت‌های بلندمدت (شوخی‌های جاری گروه)."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from .ai import AIClient
from .config import Settings

log = logging.getLogger("astolfo.memory")

MAX_NOTES_CHARS = 900
SUMMARY_BATCH = 8


@dataclass
class ChatState:
    chat_id: int
    history: Deque[dict]
    notes: str = ""
    participants: "collections.OrderedDict[str, float]" = field(
        default_factory=collections.OrderedDict
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_reply_at: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)
    reply_chance: Optional[float] = None
    forced_mode: Optional[str] = None
    muted: bool = False
    turn_count: int = 0
    replies_sent: int = 0
    summarizing: bool = False
    title: str = ""

    def touch_participant(self, name: str) -> None:
        self.participants[name] = time.time()
        self.participants.move_to_end(name)
        while len(self.participants) > 20:
            self.participants.popitem(last=False)

    def recent_texts(self, count: int = 6) -> List[str]:
        return [
            str(turn.get("content", ""))
            for turn in list(self.history)[-count:]
            if isinstance(turn.get("content"), str)
        ]

    def add_user(self, name: str, text: str) -> None:
        self.history.append({"role": "user", "content": f"{name}: {text}"})
        self.turn_count += 1
        self.touch_participant(name)

    def add_assistant(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})
        self.turn_count += 1
        self.replies_sent += 1


class ChatStore:
    """نگهدارندهٔ وضعیت چت‌ها با پاک‌سازی TTL/LRU و ذخیرهٔ تنظیمات روی دیسک."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._chats: "collections.OrderedDict[int, ChatState]" = collections.OrderedDict()
        self._path = os.path.join(settings.data_dir, "state.json")
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------
    def get(self, chat_id: int) -> ChatState:
        state = self._chats.get(chat_id)
        if state is None:
            state = ChatState(
                chat_id=chat_id,
                history=collections.deque(maxlen=self._s.max_history_len),
            )
            self._chats[chat_id] = state
        state.last_seen = time.monotonic()
        self._chats.move_to_end(chat_id)
        self._evict()
        return state

    def all_states(self) -> List[ChatState]:
        return list(self._chats.values())

    def mark_dirty(self) -> None:
        self._dirty = True

    def _evict(self) -> None:
        now = time.monotonic()
        stale = [
            cid
            for cid, st in self._chats.items()
            if now - st.last_seen > self._s.chat_ttl_sec and not st.lock.locked()
        ]
        for cid in stale:
            self._chats.pop(cid, None)

        while len(self._chats) > self._s.max_chats:
            cid, st = self._chats.popitem(last=False)
            if st.lock.locked():  # چتی که وسط تولید پاسخ است دور ریخته نمی‌شود
                self._chats[cid] = st
                self._chats.move_to_end(cid)
                break

    # ------------------------------------------------------------------
    # ذخیره‌سازی (فقط تنظیمات و یادداشت‌ها؛ متن پیام‌ها روی دیسک نمی‌رود)
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("خواندن حافظهٔ ذخیره‌شده شکست خورد: %s", exc)
            return

        for raw_id, blob in (data.get("chats") or {}).items():
            try:
                chat_id = int(raw_id)
            except ValueError:
                continue
            state = ChatState(
                chat_id=chat_id,
                history=collections.deque(maxlen=self._s.max_history_len),
                notes=str(blob.get("notes") or "")[:MAX_NOTES_CHARS],
                reply_chance=blob.get("reply_chance"),
                forced_mode=blob.get("forced_mode"),
                muted=bool(blob.get("muted")),
                title=str(blob.get("title") or ""),
            )
            self._chats[chat_id] = state
        log.info("تنظیمات %d چت از دیسک بازیابی شد.", len(self._chats))

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        payload = {
            "chats": {
                str(cid): {
                    "notes": st.notes,
                    "reply_chance": st.reply_chance,
                    "forced_mode": st.forced_mode,
                    "muted": st.muted,
                    "title": st.title,
                }
                for cid, st in self._chats.items()
                if st.notes or st.reply_chance is not None or st.forced_mode or st.muted
            }
        }
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception as exc:
            log.warning("ذخیرهٔ حافظه شکست خورد: %s", exc)


# ---------------------------------------------------------------------------
# خلاصه‌سازی چرخشی: تبدیل پیام‌های قدیمی به «چیزهایی که یادم مونده»
# ---------------------------------------------------------------------------
SUMMARY_PROMPT = """\
You maintain the long-term memory notes of a persona bot inside a Telegram chat.
Merge the OLD NOTES with the NEW MESSAGES into updated notes.

Keep only what would help a member of this chat sound like they belong later:
who the regulars are and what they're into, running jokes, ongoing situations,
stated preferences, plans, and anything the bot promised.
Drop small talk, greetings, and one-off chatter.

Hard rules: write in the chat's own language, plain short lines (max 8 lines,
under 700 characters total), no markdown, and never invent anything that was not
actually said. If nothing is worth remembering, return the old notes unchanged.

Answer ONLY as JSON: {"notes": "..."}"""


async def update_notes(ai: AIClient, settings: Settings, state: ChatState) -> None:
    """پیام‌های قدیمی را به یادداشت بلندمدت تبدیل می‌کند (در پس‌زمینه)."""
    if not settings.summary_enabled or state.summarizing:
        return
    if len(state.history) < state.history.maxlen:
        return

    state.summarizing = True
    try:
        batch = [
            str(turn.get("content", ""))
            for turn in list(state.history)[:SUMMARY_BATCH]
            if isinstance(turn.get("content"), str)
        ]
        if not batch:
            return
        data = await ai.json_call(
            [
                {"role": "system", "content": SUMMARY_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"OLD NOTES:\n{state.notes or '(empty)'}\n\n"
                        f"NEW MESSAGES:\n" + "\n".join(batch)
                    ),
                },
            ],
            model=settings.model_summary,
            max_tokens=400,
        )
        if data and isinstance(data.get("notes"), str):
            notes = data["notes"].strip()[:MAX_NOTES_CHARS]
            if notes:
                state.notes = notes
                log.debug("یادداشت چت %s به‌روزرسانی شد.", state.chat_id)
                return
    except Exception as exc:  # pragma: no cover
        log.warning("خلاصه‌سازی شکست خورد: %s", exc)
    finally:
        state.summarizing = False
