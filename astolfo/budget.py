"""Credit accounting and graceful degradation when spend approaches the cap.

Cost comes from OpenRouter's `usage.cost` field (enabled by TRACK_COST). Daily and
monthly totals are persisted so restarts do not reset the caps.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .config import Settings
from .llm import Usage

log = logging.getLogger(__name__)

FULL = "full"
CHEAP = "cheap"
ADDRESSED_ONLY = "addressed_only"
STOPPED = "stopped"

KEEP_DAYS = 35


@dataclass(frozen=True)
class Allowance:
    """What the bot is still allowed to do at the current spend level."""

    allowed: bool = True
    level: str = FULL
    reason: str = ""
    force_mode: str | None = None
    allow_web: bool = True
    allow_router_llm: bool = True

    @property
    def degraded(self) -> bool:
        return self.level != FULL


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _empty_day() -> dict:
    return {
        "cost": 0.0,
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "by_mode": {},
        "by_model": {},
        "chats": {},
        "users": {},
        "saved_by_cache": 0,
        "stars": 0,
    }


class BudgetTracker:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._path = os.path.join(settings.data_dir, "usage.json")
        self._days: dict[str, dict] = defaultdict(_empty_day)
        self._dirty = False
        self._load()

    def configure(self, settings: Settings) -> None:
        self._s = settings

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("could not read usage history: %s", exc)
            return
        for day, blob in (raw.get("days") or {}).items():
            merged = _empty_day()
            merged.update(blob)
            self._days[day] = merged
        self._prune()

    def _prune(self) -> None:
        for day in sorted(self._days)[:-KEEP_DAYS]:
            self._days.pop(day, None)

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        self._prune()
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"days": dict(self._days)}, fh, ensure_ascii=False)
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception as exc:
            log.warning("could not persist usage history: %s", exc)

    # -- recording -------------------------------------------------------
    def record(
        self,
        *,
        mode: str,
        model: str,
        usage: Usage,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        day = self._days[_today()]
        day["cost"] += usage.cost
        day["calls"] += 1
        day["prompt_tokens"] += usage.prompt_tokens
        day["completion_tokens"] += usage.completion_tokens
        day["cached_tokens"] += usage.cached_tokens
        day["by_mode"][mode] = round(day["by_mode"].get(mode, 0.0) + usage.cost, 6)
        if model:
            day["by_model"][model] = round(day["by_model"].get(model, 0.0) + usage.cost, 6)
        if chat_id is not None:
            key = str(chat_id)
            day["chats"][key] = day["chats"].get(key, 0) + 1
        if user_id is not None:
            who = str(user_id)
            day["users"][who] = day["users"].get(who, 0) + 1
        self._dirty = True

    def record_donation(self, stars: int) -> None:
        self._days[_today()]["stars"] += stars
        self._dirty = True
        self.save()  # never lose a payment to a restart

    def record_cache_hit(self) -> None:
        self._days[_today()]["saved_by_cache"] += 1
        self._dirty = True

    # -- queries ---------------------------------------------------------
    def today(self) -> dict:
        return self._days[_today()]

    def today_cost(self) -> float:
        return round(self.today()["cost"], 6)

    def month_cost(self) -> float:
        prefix = date.today().strftime("%Y-%m")
        return round(sum(d["cost"] for k, d in self._days.items() if k.startswith(prefix)), 6)

    def chat_calls_today(self, chat_id: int) -> int:
        return self.today()["chats"].get(str(chat_id), 0)

    def user_calls_today(self, user_id: int) -> int:
        return self.today().get("users", {}).get(str(user_id), 0)

    def cache_hit_rate(self) -> float:
        day = self.today()
        prompt = day["prompt_tokens"]
        return round(day["cached_tokens"] / prompt, 3) if prompt else 0.0

    # -- policy ----------------------------------------------------------
    def check(
        self,
        *,
        chat_id: int,
        addressed: bool,
        user_id: int | None = None,
        chat_limit: int = 0,
        user_limit: int = 0,
    ) -> Allowance:
        """Whether this message may be answered, and how expensively.

        A limit set on one group or one person beats the global one, so a busy
        group can be capped without quieting the rest.
        """
        limit = self._s.daily_budget_usd
        monthly = self._s.monthly_budget_usd

        if monthly > 0 and self.month_cost() >= monthly:
            return Allowance(False, STOPPED, "monthly budget reached", allow_web=False)

        call_cap = chat_limit or self._s.chat_daily_call_limit
        if call_cap > 0 and self.chat_calls_today(chat_id) >= call_cap:
            return Allowance(False, STOPPED, "chat daily call limit reached", allow_web=False)

        person_cap = user_limit or self._s.user_daily_call_limit
        spent = self.user_calls_today(user_id) if user_id is not None else 0
        if person_cap > 0 and spent >= person_cap:
            return Allowance(False, STOPPED, "personal daily limit reached", allow_web=False)

        if limit <= 0:
            return Allowance()

        ratio = self.today_cost() / limit
        if ratio >= 1.2:
            return Allowance(False, STOPPED, "daily budget exhausted", allow_web=False)
        if ratio >= 1.0:
            if not addressed:
                return Allowance(False, ADDRESSED_ONLY, "over budget, replying only when addressed")
            return Allowance(
                True,
                ADDRESSED_ONLY,
                "over budget, cheap replies only",
                force_mode="fast",
                allow_web=False,
                allow_router_llm=False,
            )
        if ratio >= 0.8:
            return Allowance(
                True,
                CHEAP,
                "approaching daily budget",
                force_mode="fast",
                allow_web=False,
                allow_router_llm=False,
            )
        return Allowance()

    # -- reporting -------------------------------------------------------
    def summary(self) -> dict:
        day = self.today()
        top_model = max(day["by_model"].items(), key=lambda kv: kv[1], default=("-", 0.0))
        return {
            "cost_today": round(day["cost"], 4),
            "cost_month": self.month_cost(),
            "daily_budget": self._s.daily_budget_usd,
            "monthly_budget": self._s.monthly_budget_usd,
            "calls": day["calls"],
            "prompt_tokens": day["prompt_tokens"],
            "completion_tokens": day["completion_tokens"],
            "cached_tokens": day["cached_tokens"],
            "cache_hit_rate": self.cache_hit_rate(),
            "cache_replies": day["saved_by_cache"],
            "stars_today": day["stars"],
            "by_mode": dict(sorted(day["by_mode"].items(), key=lambda kv: -kv[1])),
            "top_model": top_model[0],
            "generated_at": time.time(),
        }
