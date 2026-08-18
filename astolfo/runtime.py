"""Shared services, assembled once and passed around through bot_data."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .budget import BudgetTracker
from .cache import TTLCache
from .config import Settings
from .crypto import SecretBox
from .db import Database, open_database
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
    db: Database
    box: SecretBox
    responses: TTLCache = field(init=False)
    # Read on every message, changed only from the panel, so it lives in memory.
    blocked: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.responses = TTLCache(maxsize=512, ttl=self.settings.response_cache_ttl)
        self.blocked = self.db.blocked_ids()

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        self.db.set_blocked(user_id, blocked)
        if blocked:
            self.blocked.add(user_id)
        else:
            self.blocked.discard(user_id)

    @classmethod
    def build(
        cls,
        settings: Settings,
        database: Database | None = None,
        box: SecretBox | None = None,
    ) -> Runtime:
        llm = LLMClient(settings)
        database = database or open_database(settings.data_dir)
        return cls(
            settings=settings,
            llm=llm,
            store=ChatStore(settings, database),
            router=Router(settings, llm),
            budget=BudgetTracker(settings),
            strings=Strings(settings.locale),
            db=database,
            box=box or SecretBox(settings.data_dir),
        )

    async def reconfigure(self, settings: Settings) -> None:
        """Adopt changed settings in place, so a key swap needs no restart.

        The chat store is deliberately kept: it holds the running conversations
        and their locks, and dropping it would make every group forget mid-reply.
        """
        previous = self.llm
        self.settings = settings
        self.llm = LLMClient(settings)
        self.store.configure(settings)
        self.router.configure(settings, self.llm)
        self.budget.configure(settings)
        self.strings = Strings(settings.locale)
        self.responses = TTLCache(maxsize=512, ttl=settings.response_cache_ttl)
        await self.llm.load_catalog()
        await previous.aclose()
        log.info("settings reloaded: %s", ", ".join(p.name for p in self.llm.providers))

    def record(self, *, mode: str, model: str, usage: Usage, chat_id: int | None = None) -> None:
        if usage.total_tokens or usage.cost:
            self.budget.record(mode=mode, model=model, usage=usage, chat_id=chat_id)

    def save(self, force: bool = False) -> None:
        self.store.save(force=force)
        self.budget.save(force=force)

    async def aclose(self) -> None:
        self.save(force=True)
        await self.llm.aclose()
        self.db.close()


def get(context) -> Runtime:
    return context.application.bot_data[KEY]
