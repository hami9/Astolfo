"""Shared services, assembled once and passed around through bot_data."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .budget import BudgetTracker
from .cache import TTLCache
from .config import Settings
from .llm import LLMClient, Usage
from .memory import ChatStore
from .routing import Router
from .strings import Strings

log = logging.getLogger(__name__)

KEY = "runtime"


@dataclass
class Runtime:
    settings: Settings
    llm: LLMClient
    store: ChatStore
    router: Router
    budget: BudgetTracker
    strings: Strings
    responses: TTLCache = field(init=False)

    def __post_init__(self) -> None:
        self.responses = TTLCache(maxsize=512, ttl=self.settings.response_cache_ttl)

    @classmethod
    def build(cls, settings: Settings) -> Runtime:
        llm = LLMClient(settings)
        return cls(
            settings=settings,
            llm=llm,
            store=ChatStore(settings),
            router=Router(settings, llm),
            budget=BudgetTracker(settings),
            strings=Strings(settings.locale),
        )

    def record(self, *, mode: str, model: str, usage: Usage, chat_id: int | None = None) -> None:
        if usage.total_tokens or usage.cost:
            self.budget.record(mode=mode, model=model, usage=usage, chat_id=chat_id)

    def save(self, force: bool = False) -> None:
        self.store.save(force=force)
        self.budget.save(force=force)

    async def aclose(self) -> None:
        self.save(force=True)
        await self.llm.aclose()


def get(context) -> Runtime:
    return context.application.bot_data[KEY]
