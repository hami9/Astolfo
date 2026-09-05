"""Shared services, assembled once and passed around through bot_data."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .attention import Attention
from .budget import BudgetTracker
from .cache import TTLCache
from .config import Settings
from .crypto import SecretBox
from .db import Database, open_database, today
from .llm import LLMClient, Usage
from .memory import ChatStore
from .roles import Roles
from .routing import Router
from .services import ServiceRegistry
from .strings import Strings
from .tuning import Credit

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
    registry: ServiceRegistry
    responses: TTLCache = field(init=False)
    # One bot, one train of thought, and one cache of who runs which group.
    attention: Attention = field(init=False)
    roles: Roles = field(default_factory=Roles)
    # Read on every message, changed only from the panel, so they live in memory.
    blocked: set[int] = field(default_factory=set)
    user_limits: dict[int, int] = field(default_factory=dict)
    dormant: set[int] = field(default_factory=set)
    # Retiring clients still draining their own requests. Held so the tasks
    # are not garbage collected mid-close.
    _retiring: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.responses = TTLCache(maxsize=512, ttl=self.settings.response_cache_ttl)
        self.attention = Attention(self.settings.attention_hold)
        self.blocked = self.db.blocked_ids()
        self.user_limits = self.db.user_limits()
        self.dormant = self.db.dormant_ids()

    def limit_for(self, user_id: int) -> int:
        """This person's own daily cap, or 0 when they follow the global one."""
        return self.user_limits.get(user_id, 0)

    def set_user_limit(self, user_id: int, limit: int) -> None:
        self.db.set_user_limit(user_id, limit)
        if limit > 0:
            self.user_limits[user_id] = limit
        else:
            self.user_limits.pop(user_id, None)

    def set_chat_off(self, chat_id: int, off: bool) -> None:
        """Switch a chat off entirely: nothing read, stored, counted or answered."""
        state = self.store.get(chat_id)
        state.off = off
        self.store.mark_dirty()
        self.store.save(force=True)
        if off:
            self.dormant.add(chat_id)
        else:
            self.dormant.discard(chat_id)

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
        database = database or open_database(settings.data_dir)
        box = box or SecretBox(settings.data_dir)
        registry = ServiceRegistry(database, box)
        llm = LLMClient(settings, registry=registry)
        return cls(
            settings=settings,
            llm=llm,
            store=ChatStore(settings, database),
            router=Router(settings, llm),
            budget=BudgetTracker(settings),
            strings=Strings(settings.locale),
            db=database,
            box=box,
            registry=registry,
        )

    async def reconfigure(self, settings: Settings) -> None:
        """Adopt changed settings in place, so a key swap needs no restart.

        The chat store is deliberately kept: it holds the running conversations
        and their locks, and dropping it would make every group forget mid-reply.
        """
        previous = self.llm
        self.settings = settings
        self.llm = LLMClient(settings, registry=self.registry)
        self.store.configure(settings)
        self.router.configure(settings, self.llm)
        self.budget.configure(settings)
        self.strings = Strings(settings.locale)
        self.responses = TTLCache(maxsize=512, ttl=settings.response_cache_ttl)
        self.attention.configure(settings.attention_hold)
        await self.llm.load_catalog()
        # Not awaited, and not closed outright. A reply already in flight still
        # holds the old client, and closing it under them is what let every panel
        # press kill a turn with "Cannot send a request, as the client has been
        # closed". The press returns now; those pools close when the last request
        # using them finishes.
        retiring = asyncio.create_task(previous.aclose())
        self._retiring.add(retiring)
        retiring.add_done_callback(self._retiring.discard)
        log.info("settings reloaded: %s", ", ".join(p.name for p in self.llm.providers))

    def record(
        self,
        *,
        mode: str,
        model: str,
        usage: Usage,
        chat_id: int | None = None,
        user_id: int | None = None,
        service: str = "",
        variant: str = "",
        latency_ms: int = 0,
        repaired: bool = False,
        broken: str = "",
    ) -> None:
        """Fold one model call into the running totals.

        The budget has always been told what a call cost. The outcomes table is
        the other half: which service, model and prompt produced the reply, and
        whether it arrived usable. Without a service there is nothing to file it
        under - the router and the failed turns - so only the spend is counted.
        """
        if usage.total_tokens or usage.cost:
            self.budget.record(
                mode=mode, model=model, usage=usage, chat_id=chat_id, user_id=user_id
            )
        if not service:
            return
        self.db.add_outcome(
            today(),
            service=service,
            model=model,
            variant=variant,
            mode=mode,
            calls=1,
            repaired=int(repaired),
            broken=int(bool(broken)),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost=usage.cost,
            latency_ms=latency_ms,
        )

    def credit_answer(self, credit: Credit) -> None:
        """Somebody answered the bot. Credit whatever produced what they answered."""
        if not credit.known:
            return
        self.db.add_outcome(
            today(),
            service=credit.service,
            model=credit.model,
            variant=credit.variant,
            mode=credit.mode,
            answered=1,
        )

    def save(self, force: bool = False) -> None:
        self.store.save(force=force)
        self.budget.save(force=force)

    async def aclose(self) -> None:
        self.save(force=True)
        await self.llm.aclose()
        self.db.close()


def get(context) -> Runtime:
    return context.application.bot_data[KEY]
