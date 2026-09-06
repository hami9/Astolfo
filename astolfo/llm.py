"""OpenRouter chat client: retries, model fallbacks, reasoning control, web search."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import catalog, faults
from . import providers as providers_mod
from .catalog import Model
from .config import Settings

log = logging.getLogger(__name__)

# OpenRouter rejects a longer `models` chain outright.
MAX_FALLBACKS = 3

# How long a free model is skipped after it turns us away. A per-minute limit
# and a broken model both clear soon; an exhausted daily quota does not.
RATE_LIMIT_COOLDOWN = 600.0
EMPTY_COOLDOWN = 600.0
# What a model earns for going on producing nothing. Ten minutes was one
# strike, and a model that answers with silence all day came straight back
# twenty times in one log. Escalating rather than a blocklist, because the
# free pool is discovered: tomorrow the useless one is a different id.
EMPTY_STRIKES = (EMPTY_COOLDOWN, 3600.0, 12 * 3600.0)
# How far a model's record may sink it in the queue, mirroring the +3 a
# working model can earn. It is an ordering, not a ban: a model that was
# useless yesterday is still tried, just last.
MAX_SINK = 3
QUOTA_COOLDOWN = 6 * 3600.0
# A refused key is a configuration problem; asking again this run cannot fix it.
AUTH_COOLDOWN = 24 * 3600.0
# A 403 is not the same claim as a 401. 401 says this key is not valid; 403 says
# not right now - a balance that dipped, a policy, a regional hiccup - and the
# same key often works minutes later. OpenRouter answered a panel test while the
# bot was still sitting out a day-long rest earned by one 403.
FORBIDDEN_COOLDOWN = 600.0

# A free-tier 429 is account-wide, so every model is equally unavailable and the
# only useful response is for the whole bot to stop knocking for a while.
ACCOUNT_PAUSE = 60.0

# How many models one service may disown before the fact is about the account
# rather than the models. Sixteen ids from six vendors did not stop existing in
# the two minutes it took to ask them: a new key that cannot reach the free tier
# is refused for every one of them, and writing that down sixteen times empties
# a pool that was never the problem.
ACCOUNT_DISOWNS = 3

# Windows too wide to retry in place. A per-minute ceiling is worth sitting out
# with a backoff; a daily or monthly one is not, whether or not free mode is on.
SLOW = (faults.DAY, faults.MONTH)

# The last few refusals from each service, so the panel can print what they
# actually said rather than a status code. In memory: these are for reading now,
# and a service that has been quiet for an hour has nothing to explain.
FAULTS_KEPT = 8

# How many of a service's own ids to take when every configured one is gone. The
# list is walked on failover, so it stays short whatever the service lists;
# everything else is still in the catalog and selectable by hand.
ADOPT_AT_MOST = 6

# How long a retiring client waits for its own requests before closing anyway.
# Long enough for a think call with reasoning to finish; short enough that a
# wedged request cannot hold connection pools open for the life of the process.
DRAIN_TIMEOUT = 90.0


@dataclass(frozen=True)
class Citation:
    title: str
    url: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResult:
    text: str | None = None
    model: str = ""
    # Which service answered, and how long its call took. In free mode the model
    # changes turn to turn, so a reply means nothing without knowing who served
    # it; both are filled in by the caller that made the successful request.
    service: str = ""
    latency_ms: int = 0
    citations: list[Citation] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    error_kind: str | None = None  # "payment" | "auth" | None

    @property
    def ok(self) -> bool:
        return bool(self.text)


def cacheable_system(text: str, model: str, enabled: bool) -> dict[str, Any]:
    """System message with an explicit cache breakpoint where the provider supports it.

    Anthropic models need an explicit `cache_control` marker; Gemini and OpenAI cache
    stable prefixes implicitly, so plain text is enough for them.
    """
    if enabled and model.startswith("anthropic/"):
        return {
            "role": "system",
            "content": [
                {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
            ],
        }
    return {"role": "system", "content": text}


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        registry=None,
    ) -> None:
        self._s = settings
        self._transport = transport
        self._registry = registry
        stored = registry.rows() if registry else None
        self.providers = providers_mod.discover(
            settings.providers, fallback_key=settings.api_key, stored=stored
        )
        if not self.providers:
            # Nothing named a key, so fall back to the plain single-service setup.
            self.providers = [
                providers_mod.Provider(
                    name="openrouter",
                    base_url=settings.api_base,
                    credentials=[providers_mod.Credential(value=settings.api_key)],
                    key_env="OPENROUTER_API_KEY",
                    discovers_free_models=True,
                    openrouter_extensions=True,
                )
            ]
        self._restore_rests(stored)
        for unknown in providers_mod.unknown_names(settings.providers, stored):
            log.warning("unknown provider %r in PROVIDERS, ignoring", unknown)
        if len(self.providers) > 1:
            log.info("providers: %s", ", ".join(p.name for p in self.providers))

        self._clients = {p.name: self._build_client(p) for p in self.providers}
        self._client = self._clients[self.providers[0].name]
        self._catalog: set | None = None
        self._models: list[Model] = []
        self._free_text: list[str] = []
        self._free_vision: list[str] = []
        self._free_audio: list[str] = []
        self._cooldowns: dict[str, float] = {}
        # (service, model) pairs that answered a photo with "image content is not
        # supported". Keyed by both because it is a fact about one model, not
        # about its service: one refusal from a free model used to mark the whole
        # of OpenRouter blind, which took its real vision models down with it and
        # did nothing about the model that had actually refused.
        self._text_only: set[tuple[str, str]] = set()
        # (service, model) pairs the service has said outright it does not serve.
        # Kept for the life of the process for the same reason as the pair above:
        # it is a fact about the pair, and asking again only buys the same answer.
        self._unknown: set[tuple[str, str]] = set()
        # service -> the last few refusals, oldest first.
        self._faults: dict[str, list[tuple[float, faults.Fault]]] = {}
        self._scores: dict[str, int] = {}
        # How many times each model has come back unusable - carried across
        # restarts, because the free pool is ordered by widest context and the
        # widest one is not always a working one. Without this, every restart put
        # the worst-behaved model back at the front with a clean sheet.
        self._strikes: dict[str, int] = {}
        self._restore_strikes()
        # One lock per service, because the clock is per service. A single lock
        # held across the sleep made a turn bound for an idle service wait out a
        # busy one's whole gap - measured at a full gap owed by a provider that
        # owed nothing.
        self._pace_locks: dict[str, asyncio.Lock] = {}
        # Requests using these connection pools right now, so a client being
        # retired can wait for them instead of closing under them.
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def _build_client(self, provider: providers_mod.Provider) -> httpx.AsyncClient:
        # No Authorization here: a service can hold several keys, so the header
        # belongs to the request rather than to the connection pool.
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._s.request_timeout, connect=20.0),
            headers={
                "Content-Type": "application/json",
                "HTTP-Referer": self._s.app_url,
                "X-Title": self._s.app_title,
            },
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=15),
        )

    @staticmethod
    def _auth(credential: providers_mod.Credential) -> dict[str, str]:
        return {"Authorization": f"Bearer {credential.value}"}

    def _rest_credential(
        self, credential: providers_mod.Credential, seconds: float, error: str
    ) -> None:
        credential.rested_until = time.time() + seconds
        credential.last_error = error
        if self._registry:
            self._registry.rest_credential(credential.id, seconds, error)
            self._registry.note_use(credential.id, failed=True)

    def _note_result(
        self,
        provider: providers_mod.Provider,
        credential: providers_mod.Credential,
        result: ChatResult,
    ) -> None:
        """Book the call against the key that made it and the service it went to."""
        if not self._registry:
            return
        credential.last_error = ""
        self._registry.note_use(credential.id)
        self._registry.note_ok(credential.id)
        self._revive(provider, credential)
        self._registry.record_call(
            provider.name, tokens=result.usage.total_tokens, cost=result.usage.cost
        )

    def _revive(
        self,
        provider: providers_mod.Provider,
        credential: providers_mod.Credential | None = None,
        *,
        force: bool = False,
    ) -> None:
        """It answered, so stop treating it as broken.

        A refused key used to rest the service for a day and nothing ever
        cancelled that, however the reason went away - a balance topped up, a
        block lifted, a key replaced. The panel's own test could get an answer
        and the bot would still ignore the service for the rest of the day.
        Written only when something was actually resting, so the ordinary
        successful call costs no database write.
        """
        # `force` because the panel's test clears the pause before making the
        # call, so by the time the answer arrives there is nothing left in memory
        # to say it had been resting - and the stored rest would outlive it.
        resting = force or provider.paused_until > time.monotonic()
        provider.paused_until = 0.0
        if resting and self._registry:
            log.info("%s answered, so it is back in the rotation", provider.name)
            self._registry.revive_service(provider.name)
        if credential is not None and credential.rested_until:
            credential.rested_until = 0.0
            if self._registry:
                self._registry.revive_credential(credential.id)

    def _restore_strikes(self) -> None:
        """Carry what each model has already been caught doing into this run."""
        if not self._registry:
            return
        try:
            strikes = dict(self._registry.model_strikes())
            resting = dict(self._registry.model_rests())
        except Exception as exc:
            log.warning("could not read model health: %s", exc)
            return
        self._strikes = strikes
        for model, count in strikes.items():
            self._scores[model] = -min(count, MAX_SINK)
        # Stored as wall clock, compared here against the monotonic clock the
        # rest of the client uses, exactly as service rests are.
        now, monotonic = time.time(), time.monotonic()
        for model, until in resting.items():
            remaining = until - now
            if remaining > 0:
                self._cooldowns[model] = monotonic + remaining
                log.info("%s is still resting for %.0f minutes", model, remaining / 60)
        if strikes:
            log.info(
                "%d model(s) start behind on their record: %s",
                len(strikes),
                ", ".join(sorted(strikes)[:6]),
            )

    def _restore_rests(self, stored: list[dict] | None) -> None:
        """Carry stored rest windows into this run.

        They are kept as wall clock and compared here against the monotonic clock
        the rest of the client uses, so a quota that runs until tomorrow is still
        known after a restart tonight.
        """
        if not stored:
            return
        now, monotonic = time.time(), time.monotonic()
        by_name = {row["name"]: row for row in stored}
        for provider in self.providers:
            remaining = float(by_name.get(provider.name, {}).get("rested_until") or 0.0) - now
            if remaining > 0:
                provider.paused_until = monotonic + remaining
                log.info(
                    "%s is still resting for %.0f minutes", provider.name, remaining / 60
                )

    def _live_providers(self) -> list[providers_mod.Provider]:
        """Services that could take a request now, in the order they are tried.

        Pinning one is a deliberate choice to stop failing over: if it is out of
        allowance the turn fails rather than quietly spending somewhere else.
        """
        now = time.monotonic()
        usable = [p for p in self.providers if p.paused_until <= now]
        pinned = (self._s.pinned_service or "").strip().lower()
        if pinned:
            return [p for p in usable if p.name == pinned]
        return usable

    def _pause_provider(
        self, provider: providers_mod.Provider, seconds: float, error: str = ""
    ) -> None:
        provider.paused_until = max(provider.paused_until, time.monotonic() + seconds)
        if self._registry:
            self._registry.rest_service(provider.name, seconds, error)
        others = [p.name for p in self._live_providers()]
        log.warning(
            "%s is out of allowance, resting it for %.0fs%s",
            provider.name,
            seconds,
            f"; falling back to {', '.join(others)}" if others else "; nothing left to try",
        )

    async def aclose(self) -> None:
        """Close the connection pools once nothing is still using them.

        Closing while a request is in flight raises "Cannot send a request, as
        the client has been closed" inside whichever chat was mid-reply. That
        happened on every panel press: `Runtime.reconfigure` builds a new client
        and retires this one, and any turn already awaiting a response still
        holds it.
        """
        # Re-checked rather than waited on once. A turn hands over between two
        # services by releasing the marker and taking it again on the next line:
        # the release sets `_idle` and wakes this, the handover is synchronous so
        # nothing else can run in between, and waking was enough to close the
        # pools under a turn that had already taken the marker back.
        deadline = time.monotonic() + DRAIN_TIMEOUT
        while self._inflight:
            log.info("waiting for %d request(s) before closing", self._inflight)
            left = deadline - time.monotonic()
            if left <= 0:
                log.warning(
                    "%d request(s) still running after %.0fs; closing anyway",
                    self._inflight, DRAIN_TIMEOUT,
                )
                break
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=left)
            except (TimeoutError, asyncio.TimeoutError):
                continue  # the deadline check above decides whether to give up
        for client in self._clients.values():
            await client.aclose()

    async def probe(self, name: str) -> tuple[bool, str]:
        """Ask one service a throwaway question to see whether its key works.

        Reported separately from "it answered", because a key can be perfectly
        valid and still be out of quota or rate limited this minute.
        """
        provider = next((p for p in self.providers if p.name == name), None)
        if provider is None:
            return False, "no key configured for this service"

        was_paused, provider.paused_until = provider.paused_until, 0.0
        result = ChatResult(error="the test did not run")
        try:
            result = await self._chat_with(
                provider,
                [{"role": "user", "content": "ping"}],
                model=self._s.model_fast,
                temperature=0.0,
                max_tokens=5,
                reasoning=None,
                web=False,
                response_format=None,
                fallbacks=False,
                max_retries=1,
                vision=False,
                audio=False,
            )
        finally:
            if not result.ok:
                # Still unwell, so it goes back to resting where it was. A
                # successful test is left alone: `_note_result` has already put
                # the service back in the rotation, which is the whole point of
                # pressing test on something the bot has given up on.
                provider.paused_until = max(was_paused, provider.paused_until)

        if result.ok:
            self._revive(provider, force=was_paused > time.monotonic())
            return True, f"answered by {result.model}"
        if result.error_kind == "auth":
            return False, "the key was refused"
        if result.error_kind == "blocked":
            # The service never saw the request, so it says nothing about the key.
            return False, "blocked before it reached the service - the network, not the key"
        if result.error_kind == "payment":
            return True, "the key works but has no credit or quota left"
        if result.error_kind == "throttled":
            return True, "the key works but is rate limited right now"
        return False, result.error or "no answer"

    # -- allowances -------------------------------------------------------
    def usable_now(self) -> bool:
        """True while at least one service could take a request this moment."""
        return bool(self._live_providers())

    def throttled_for(self) -> float:
        """Seconds until any service is usable again, 0 while one still is."""
        now = time.monotonic()
        remaining = [p.paused_until - now for p in self.providers]
        return max(0.0, min(remaining)) if remaining else 0.0

    async def _pace(self, provider: providers_mod.Provider) -> None:
        """Space requests out so a per-minute allowance is not blown.

        Every chat draws on the same account budget, so the gap is shared across
        chats, but each service keeps its own clock.
        """
        rpm = self._s.free_rpm
        if not self._s.free_mode or rpm <= 0:
            return
        gap = 60.0 / rpm
        lock = self._pace_locks.setdefault(provider.name, asyncio.Lock())
        async with lock:
            wait = provider.last_request + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            provider.last_request = time.monotonic()

    # -- model catalog ---------------------------------------------------
    async def _listing(self, provider: providers_mod.Provider) -> list[dict]:
        """What this service says it offers, or an empty list if it will not say."""
        try:
            credential = provider.pick(time.time())
            headers = self._auth(credential) if credential else {}
            async with self._in_flight():
                resp = await self._clients[provider.name].get(
                    provider.models_url, timeout=20.0, headers=headers
                )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log.info(
                "%s did not list its models (%s); using the configured ids", provider.name, exc
            )
            return []
        entries = payload.get("data") if isinstance(payload, dict) else payload
        return [e for e in (entries or []) if isinstance(e, dict) and e.get("id")]

    async def _confirm_models(self, provider: providers_mod.Provider, entries: list[dict]) -> None:
        """Reconcile the configured ids with what the service actually offers.

        Everything a service lists reaches the catalog, so the panel can offer it
        and its window is known. What is *called*, though, stays deliberately
        short: this list is walked on failover, and a service listing four
        hundred models should not become a four-hundred-deep retry chain.

        So configured ids that still exist are kept as they are, and adoption
        happens only where it is needed - a service that has renamed everything
        used to be left with a stale list answering 404 to every message.
        """
        offered = {str(e["id"]) for e in entries}
        if not offered:
            return

        # Services vary on whether ids carry a namespace prefix.
        def known(model: str) -> bool:
            return model in offered or f"models/{model}" in offered

        kept = [m for m in provider.models if known(m)]
        dropped = [m for m in provider.models if not known(m)]
        if dropped:
            log.warning("%s does not offer %s", provider.name, ", ".join(dropped))
        if kept:
            provider.models = kept
            provider.vision_models = [m for m in provider.vision_models if known(m)]
            return

        usable = catalog.read(entries, service=provider.name)
        if not usable:
            log.warning(
                "%s offers nothing that can chat; leaving its configured ids alone",
                provider.name,
            )
            return
        provider.models = [m.id for m in usable[:ADOPT_AT_MOST]]
        provider.vision_models = [m.id for m in usable[:ADOPT_AT_MOST] if m.vision]
        log.warning(
            "%s offers none of the configured models; using its own instead: %s",
            provider.name,
            ", ".join(provider.models),
        )

    async def load_catalog(self) -> None:
        """Ask every service what it offers, and merge the answers into one catalog.

        Until now exactly one service was ever asked - whichever advertised a free
        catalog, in practice OpenRouter - so `context_window` returned 0 for every
        model on the other twelve and the history budget was a guess for all of
        them, permanently.
        """
        found: list[Model] = []
        rich_ids: set[str] = set()

        for provider in self.providers:
            if not provider.discovers:
                continue
            entries = await self._listing(provider)
            if not entries:
                continue
            if not provider.discovers_free_models:
                await self._confirm_models(provider, entries)
            else:
                rich_ids |= {str(e["id"]) for e in entries}
            found.extend(
                catalog.read(entries, service=provider.name, free_tier=provider.free_tier)
            )

        # The id set is what `resolve` validates a configured model against, and
        # it may only carry the service that discovers free models: elsewhere an
        # unknown id is the operator's choice, not a mistake to correct.
        self._catalog = rich_ids or None
        if not found:
            log.warning("no service listed its models; using the configured ids")
            self._seed_free_models()
            return

        log.info(
            "catalog: %d models across %s",
            len(found),
            ", ".join(sorted({m.service for m in found})),
        )
        self._index_free_models(found)
        self._remember(self._models)

    def _remember(self, models: list[Model]) -> None:
        """Note what has been listed, so the panel can say what is actually new.

        Kept in the database rather than in memory: without it every model would
        look new after a restart, which is the same as saying nothing.
        """
        if not self._registry:
            return
        try:
            fresh = self._registry.note_models(
                [(m.service, m.id, m.context, m.free, m.vision) for m in models]
            )
        except Exception as exc:
            log.warning("could not record the catalog listing: %s", exc)
            return
        if fresh:
            log.info("%d model(s) offered for the first time: %s",
                     len(fresh), ", ".join(fresh[:8]))

    _is_free = staticmethod(catalog.is_free)
    _is_chat = staticmethod(catalog.is_chat)

    def context_window(self, model: str, service: str = "") -> int:
        """Tokens this model can hold, or 0 when no service ever named it.

        A service is preferred when given, because two services can offer the
        same id with different windows; without one the longest match wins,
        which is the order `_models` is already in.
        """
        for entry in self._models:
            if entry.id == model and (not service or entry.service == service):
                return entry.context
        return 0

    def known_models(self) -> list[Model]:
        return list(self._models)

    def models_offered(
        self, *, free_only: bool = True, vision: bool = False, service: str = ""
    ) -> list[Model]:
        """The chat models the catalog listed, for the panel to show and choose from."""
        return [
            m
            for m in self._models
            if (m.free or not free_only)
            and (m.vision or not vision)
            and (not service or m.service == service)
        ]

    def _index_free_models(self, models: list[Model]) -> None:
        """Discover zero-cost models instead of shipping a list that goes stale."""
        # Longest context first, and one entry per id: several services offer the
        # same model and the pool wants it once.
        seen: dict[str, Model] = {}
        for model in sorted(models, key=lambda m: (-m.context, m.id)):
            seen.setdefault(model.id, model)
        self._models = list(seen.values())
        free = [m for m in self._models if m.free]
        # Longest context first: the persona prompt alone is a few thousand tokens.
        self._free_text = [m.id for m in free]
        self._free_vision = [m.id for m in free if m.vision]
        self._free_audio = [m.id for m in free if m.audio]
        if self._s.free_mode:
            log.info(
                "free mode: %d chat models available (%d read images, %d hear sound)",
                len(self._free_text),
                len(self._free_vision),
                len(self._free_audio),
            )
            if not self._free_text:
                log.error("free mode is on but no zero-cost chat model was found")

    def _seed_free_models(self) -> None:
        """Fall back to the configured list when the catalog cannot be read."""
        self._models = []
        self._free_text = list(self._s.free_models)
        self._free_vision = []
        self._free_audio = []

    def _all_free(
        self, *, vision: bool = False, audio: bool = False, service: str = ""
    ) -> list[str]:
        configured = list(self._s.free_models)
        if configured:
            return configured
        if audio:
            names = list(self._free_audio)
        else:
            names = list(self._free_vision if vision else self._free_text)
        if not service:
            return names
        # A model belongs to the service that listed it. Now that every service
        # is read, an unfiltered pool would offer OpenRouter one of Google's ids.
        mine = {m.id for m in self._models if m.service == service}
        return [name for name in names if name in mine]

    def _usable(self, candidates: list[str]) -> list[str]:
        """The ones worth asking first: resting models dropped, bad ones sunk.

        The discovered pool has always been ordered this way. A service that
        names its own models was not, so the escalating rest a broken model
        earns applied to OpenRouter and nowhere else - and a retry after an
        unusable reply asked the same model again.
        """
        now = time.monotonic()
        available = [m for m in candidates if self._cooldowns.get(m, 0.0) <= now]
        # If every one is resting, try them anyway rather than going silent.
        pool = available or candidates
        # Stable sort: models that have misbehaved sink, the rest keep catalog order.
        return sorted(pool, key=lambda m: -self._scores.get(m, 0))

    def free_pool(
        self, *, vision: bool = False, audio: bool = False, service: str = ""
    ) -> list[str]:
        """Usable free models, worst-behaved last, skipping any that are resting.

        Models a service has disowned are dropped here rather than at each call
        site. `_candidates` guarded the first pick and nothing else, so `resolve`
        and every failover step kept handing back an id the service had already
        refused - which is how "it will not be asked again" was logged twice in
        two minutes about the same model.
        """
        pool = self._served(self._all_free(vision=vision, audio=audio, service=service))
        return self._usable(pool)

    def supports_free_vision(self) -> bool:
        return bool(self._all_free(vision=True))

    def supports_free_audio(self) -> bool:
        return bool(self._all_free(audio=True))

    @contextlib.asynccontextmanager
    async def _in_flight(self):
        """Hold the pools open for the duration of one request."""
        self._inflight += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._idle.set()

    def _known(self, service: str, models: list[str]) -> list[str]:
        """The ones this service has not already said it does not serve."""
        return [m for m in models if (service, m) not in self._unknown]

    def _disowns_everything(self, provider: providers_mod.Provider, why: str) -> bool:
        """Whether this service has refused so many ids that the account is the fact.

        Models do not stop existing in batches. A key that cannot reach a free
        tier is refused for every id on it, and writing that down once per model
        empties a pool that was never the problem - so past the threshold the
        service rests instead, and the models are given back.
        """
        disowned = {m for service, m in self._unknown if service == provider.name}
        if len(disowned) < ACCOUNT_DISOWNS:
            return False
        # Rest the service, but keep what each model taught. Clearing the set here
        # handed the whole pool back a minute later to be refused again: ninety-nine
        # disowned warnings against thirty-three resets, exactly three per cycle,
        # for three hours. A pause is not an amnesty.
        self._pause_provider(provider, ACCOUNT_PAUSE, why[:200])
        # One model is let out of the set, so the way back is a single probe rather
        # than the whole pool at once. If the account really is the problem, that
        # costs one call a minute instead of three.
        probe = sorted(disowned)[0]
        self._unknown.discard((provider.name, probe))
        log.warning(
            "%s refused %d of its own models; resting it and probing with %s when it wakes",
            provider.name, len(disowned), probe,
        )
        return True

    def _listed_by(self, service: str) -> set[str]:
        """Every id this service put its own name to.

        A model id is a string from a third party, and the payload is the last
        place to take one on trust: a configured `fallback_models` list is not
        filtered by service at all, so this is checked rather than assumed.
        """
        return {m.id for m in self._models if m.service == service}

    def _served(self, models: list[str]) -> list[str]:
        """The same, for a pool whose caller did not name a service.

        `_unknown` is keyed by `(service, model)` and the free pool is often asked
        for without a service at all, so the key comes from the model itself: the
        catalog already records who listed each id, which is what keeps one
        service from being offered another's.
        """
        if not self._unknown:
            return models
        owner = {m.id: m.service for m in self._models}
        return [m for m in models if (owner.get(m, ""), m) not in self._unknown]

    def _sighted(self, service: str, models: list[str]) -> list[str]:
        """The ones this service has not already refused an image on."""
        return [m for m in models if (service, m) not in self._text_only]

    def blind_to(self, service: str, model: str) -> bool:
        return (service, model) in self._text_only

    def can_see(self, provider: providers_mod.Provider, *, audio: bool = False) -> bool:
        """Whether this service still has a model that has not refused an image.

        Asked before the request rather than after, and separately from
        `_candidates`, which always falls back to the requested id and so can
        never answer "nothing here works".
        """
        if provider.discovers_free_models:
            pool = self.free_pool(vision=True, audio=audio) or self.free_pool()
        else:
            pool = list(provider.vision_models or provider.models)
        return bool(self._sighted(provider.name, pool))

    def _note_fault(
        self, resp, provider: providers_mod.Provider, model: str
    ) -> faults.Fault:
        """Read a refusal, log it in the service's own words, and keep it.

        One place, so the line in the log, the rest that is taken and the line
        on the panel are all the same fact rather than three readings of it.
        """
        raw = resp.headers.get("retry-after")
        try:
            asked = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            asked = 0.0
        fault = faults.read(
            resp.status_code,
            resp.text[:1500],
            service=provider.name,
            model=model,
            retry_after=asked,
        )
        log.warning("%s", fault.summary)
        kept = self._faults.setdefault(provider.name, [])
        kept.append((time.time(), fault))
        del kept[:-FAULTS_KEPT]
        return fault

    def recent_faults(self, service: str = "") -> list[tuple[float, faults.Fault]]:
        """What each service last refused, newest first, for the panel."""
        if service:
            return list(reversed(self._faults.get(service, [])))
        merged = [pair for kept in self._faults.values() for pair in kept]
        return sorted(merged, key=lambda pair: -pair[0])

    def stuck_on(self, model: str) -> bool:
        """Whether another go would land on this same model anyway.

        The free-mode retry exists to try a *different* model. When the pool has
        come down to one, every service's first choice is the same id, and the
        retry spends a call to be told the same thing - which is what filled a
        log with the same model rested and re-used four times in forty seconds.
        """
        if not model:
            return False
        for provider in self._live_providers():
            options = self._candidates(provider, model)
            if any(option != model for option in options):
                return False
        return True

    def _rest(self, model: str, seconds: float) -> None:
        self._cooldowns[model] = time.monotonic() + seconds

    def mark_unusable(self, model: str, seconds: float | None = None) -> None:
        """Retire a model the caller judged unusable, e.g. it wrote nonsense.

        Each further offence costs it more: ten minutes, then an hour, then the
        rest of the day. One fixed cooldown meant a model that was simply broken
        today rejoined the pool every ten minutes and wasted a turn each time.
        """
        if not model:
            return
        if self._cooldowns.get(model, 0.0) > time.monotonic():
            # Already resting and used anyway, because it was the only thing left.
            # A forced turn is not new evidence: without this the count climbed
            # from seven to ten in forty seconds and said nothing about anything.
            log.info("%s is already resting; the strike is not counted twice", model)
            return
        strikes = self._strikes[model] = self._strikes.get(model, 0) + 1
        if self._registry:
            # Written now rather than on shutdown: the process being killed is
            # exactly when this is worth having.
            try:
                strikes = self._registry.note_strike(model) or strikes
            except Exception as exc:
                log.debug("could not record the strike against %s: %s", model, exc)
        earned = EMPTY_STRIKES[min(strikes, len(EMPTY_STRIKES)) - 1]
        rest = earned if seconds is None else seconds
        self._rest(model, rest)
        if self._registry:
            # In memory alone the rest of the day lasts until the next update.
            try:
                self._registry.rest_model(model, rest)
            except Exception as exc:
                log.debug("could not record the rest for %s: %s", model, exc)
        self._scores[model] = max(-MAX_SINK, self._scores.get(model, 0) - 1)
        log.info("resting %s for %.0fs: unusable reply (strike %d)", model, rest, strikes)

    def _next_free(
        self, *, tried: set[str], vision: bool, audio: bool, service: str = ""
    ) -> str | None:
        pool = self.free_pool(vision=vision, audio=audio, service=service) or self.free_pool(
            service=service
        )
        for candidate in pool:
            if candidate not in tried:
                return candidate
        return None

    def _candidates(
        self,
        provider: providers_mod.Provider,
        requested: str,
        *,
        vision: bool = False,
        audio: bool = False,
    ) -> list[str]:
        """Everything worth asking this service for, best first."""
        if provider.discovers_free_models:
            pool = self.free_pool(
                vision=vision, audio=audio, service=provider.name
            ) or self.free_pool(service=provider.name)
            if vision:
                pool = self._sighted(provider.name, pool)
            # `requested` is the last resort, but only when this service has not
            # already said it does not have it: offering it again is how the same
            # rejection arrived seven times.
            return self._known(provider.name, pool or [requested])
        options = provider.vision_models if vision else provider.models
        found = list(options or provider.models or [requested])
        if vision:
            found = self._sighted(provider.name, found)
        return self._usable(self._known(provider.name, found))

    def _pick_model(
        self,
        provider: providers_mod.Provider,
        requested: str,
        *,
        vision: bool = False,
        audio: bool = False,
    ) -> str:
        """What to ask this particular service for.

        Only the service that publishes a catalog can be asked for free models by
        discovery; the rest answer with whatever their configuration names.
        """
        found = self._candidates(provider, requested, vision=vision, audio=audio)
        return found[0] if found else ""

    def _next_model(
        self,
        provider: providers_mod.Provider,
        requested: str,
        *,
        tried: set[str],
        vision: bool,
        audio: bool,
    ) -> str | None:
        """Another model at the same service, or None when its list is used up."""
        if provider.discovers_free_models:
            return self._next_free(
                tried=tried, vision=vision, audio=audio, service=provider.name
            )
        for candidate in self._candidates(provider, requested, vision=vision, audio=audio):
            if candidate not in tried:
                return candidate
        return None

    def resolve(self, model: str, *, vision: bool = False, audio: bool = False) -> str:
        if self._s.free_mode:
            pool = self.free_pool(vision=vision, audio=audio) or self.free_pool()
            if pool:
                return pool[0]
            log.warning("free mode is on but no free model is known; using %s", model)
            return model
        if not self._catalog or model in self._catalog:
            return model
        for candidate in self._s.fallback_models:
            if candidate in self._catalog:
                log.warning("model %s unavailable, using %s", model, candidate)
                return candidate
        log.warning("model %s unavailable and no fallback matched", model)
        return model

    # -- requests --------------------------------------------------------
    def _payload(
        self,
        *,
        provider: providers_mod.Provider,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        reasoning: dict | None,
        web: bool,
        response_format: dict | None,
        fallbacks: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        if not provider.openrouter_extensions:
            # A plain OpenAI-compatible service rejects unknown fields, so the
            # extras below are only meaningful where they are understood.
            return payload

        if fallbacks:
            # OpenRouter tries these in order when the first is rate limited or
            # errors, which is what keeps a rationed free model usable. The API
            # rejects more than MAX_FALLBACKS entries, counting the primary.
            #
            # Scoped to this service. The global pool holds every service's ids
            # now that every catalog is read, and one of Google's inside
            # OpenRouter's `models` array had it reject the whole request naming
            # that id - which the code then blamed on the model in the `model`
            # field, condemning the pool one innocent model at a time.
            alternatives = (
                self.free_pool(service=provider.name)
                if self._s.free_mode
                else self._s.fallback_models
            )
            mine = self._listed_by(provider.name)
            chain = [model] + [m for m in alternatives if m != model and m in mine]
            if len(chain) > 1:
                payload["models"] = chain[:MAX_FALLBACKS]
        if reasoning:
            payload["reasoning"] = reasoning
        if web and self._s.web_search:
            payload["plugins"] = [{"id": "web", "max_results": max(1, self._s.web_max_results)}]
        if self._s.provider_sort:
            payload["provider"] = {"sort": self._s.provider_sort}
        if self._s.track_cost:
            payload["usage"] = {"include": True}
        return payload

    @staticmethod
    def _parse(data: dict) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            return ChatResult(error="response contained no choices")

        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip() or None

        citations = []
        for annotation in message.get("annotations") or []:
            if annotation.get("type") == "url_citation":
                cite = annotation.get("url_citation") or {}
                if cite.get("url"):
                    citations.append(
                        Citation(title=(cite.get("title") or cite["url"])[:80], url=cite["url"])
                    )

        raw_usage = data.get("usage") or {}
        details = raw_usage.get("prompt_tokens_details") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
            completion_tokens=int(raw_usage.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            cost=float(raw_usage.get("cost") or 0.0),
        )
        return ChatResult(text=text, model=data.get("model", ""), citations=citations, usage=usage)

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float = 0.8,
        max_tokens: int = 400,
        reasoning: dict | None = None,
        web: bool = False,
        response_format: dict | None = None,
        fallbacks: bool = True,
        max_retries: int | None = None,
    ) -> ChatResult:
        """Try each configured service in turn; one being spent is not the end."""
        parts = [p for m in messages if isinstance(m.get("content"), list) for p in m["content"]]
        vision = any(p.get("type") == "image_url" for p in parts)
        audio = any(p.get("type") == "input_audio" for p in parts)

        live = self._live_providers()
        if not live:
            # The soonest one back, not the last: one service resting a day had
            # this say nothing would answer for a day while another was sixty
            # seconds away, which is a different decision for whoever reads it.
            # `throttled_for` below has always taken the minimum.
            waits = [p.paused_until - time.monotonic() for p in self.providers]
            return ChatResult(
                error=f"every provider is resting for another {min(waits):.0f}s",
                error_kind="throttled",
            )

        last = ChatResult(error="no provider attempted", error_kind="throttled")
        for provider in live:
            if vision and not self.can_see(provider, audio=audio):
                # Every model it would try has already refused an image. Asking
                # again is a round trip that can only end the same way.
                log.debug("skipping %s: nothing it offers takes images", provider.name)
                continue
            last = await self._chat_with(
                provider,
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning=reasoning,
                web=web,
                response_format=response_format,
                fallbacks=fallbacks,
                max_retries=max_retries,
                vision=vision,
                audio=audio,
            )
            if last.ok:
                return last
            if last.error_kind in ("throttled", "payment"):
                if provider.paused_until <= time.monotonic():
                    # Rate limited and out of credit are not the same wait. A 429
                    # clears in a minute; an empty account does not, and asking it
                    # again every minute for a day is a round trip each time that
                    # can only end the same way. A panel test brings it straight
                    # back the moment it is topped up.
                    spent = last.error_kind == "payment"
                    self._pause_provider(
                        provider, QUOTA_COOLDOWN if spent else ACCOUNT_PAUSE
                    )
                continue  # a spent service says nothing about the next one
            if last.error_kind in ("rejected", "auth", "blocked") and len(live) > 1:
                # One service disliking the request, or the key for it, says
                # nothing about the next one in line.
                log.info("trying the next service after %s declined", provider.name)
                continue
            return last
        return last

    async def _chat_with(
        self,
        provider: providers_mod.Provider,
        messages: list[dict],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        reasoning: dict | None,
        web: bool,
        response_format: dict | None,
        fallbacks: bool,
        max_retries: int | None,
        vision: bool,
        audio: bool,
    ) -> ChatResult:
        client = self._clients[provider.name]
        requested = model
        model = self._pick_model(provider, model, vision=vision, audio=audio)
        if not model:
            # Everything this service could be asked for, it has already said it
            # does not have. Move on rather than send the id back for a fifth no.
            return ChatResult(
                error=f"{provider.name} serves none of the models left to try",
                error_kind="rejected",
            )

        retries = self._s.max_retries if max_retries is None else max_retries
        if self._s.free_mode:
            # Enough attempts to walk a few models before giving up on the turn.
            retries = max(retries, min(len(self._all_free(vision=vision, audio=audio)), 5))
        # A short retry budget must not cut a service's model list short.
        retries = max(retries, len(self._candidates(provider, model, vision=vision, audio=audio)))
        # Every key at this service is worth an attempt of its own.
        retries = max(retries, len(provider.credentials))
        tried: set[str] = set()
        delay = 1.5
        last_error = "unknown"

        credential = provider.pick(time.time())
        if credential is None:
            return ChatResult(
                error=f"{provider.name} has no usable key right now", error_kind="auth"
            )

        # Held for the whole turn, not for each request. `_in_flight` used to
        # wrap only the POST, so between two attempts of one turn the count was
        # zero: a reload landing in that gap let the drain stop waiting, close
        # the pools, and the retry post to a closed client. The window is a turn
        # that already failed once and is backing off - the turn that most needs
        # the client to outlive the press.
        async with self._in_flight():
            for attempt in range(1, retries + 1):
                tried.add(model)
                await self._pace(provider)
                payload = self._payload(
                    provider=provider,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning=reasoning,
                    web=web,
                    response_format=response_format,
                    fallbacks=fallbacks,
                )
                try:
                    started = time.monotonic()
                    resp = await client.post(
                        provider.chat_url, json=payload, headers=self._auth(credential)
                    )

                    if resp.status_code == 429 or resp.status_code >= 500:
                        fault = self._note_fault(resp, provider, model)
                        last_error = fault.summary
                        if resp.status_code == 429 and (self._s.free_mode or fault.scope in SLOW):
                            # The allowance belongs to this account, so every model
                            # behind it is equally limited; the caller moves on to the
                            # next service rather than touring this one's models.
                            #
                            # And the rest is now the one the service asked for. A
                            # trial key's twenty-a-minute and a spent monthly credit
                            # both arrive as 429, and resting sixty seconds for both
                            # is what "limit, back in a minute, limit again" was.
                            self._pause_provider(
                                provider, fault.wait or ACCOUNT_PAUSE, fault.summary
                            )
                            spent = fault.kind == faults.CREDIT
                            return ChatResult(
                                error=fault.summary,
                                error_kind="payment" if spent else "throttled",
                            )
                        wait = _retry_after(resp, delay)
                        log.warning(
                            "%s (attempt %d/%d), waiting %.1fs", last_error, attempt, retries, wait
                        )
                        await asyncio.sleep(wait)
                        delay = min(delay * 2, 20.0)
                        continue

                    if resp.status_code == 400:
                        body = resp.text[:600]
                        lowered = body.lower()
                        if reasoning and "reasoning" in lowered:
                            log.warning(
                                "model %s rejected reasoning params, retrying without", model
                            )
                            reasoning = None
                            continue
                        if web and ("plugin" in lowered or "web" in lowered):
                            log.warning("web plugin rejected, retrying without search")
                            web = False
                            continue
                        if _about_the_model(lowered):
                            blamed = _names_another_model(body, model)
                            if blamed:
                                # The refusal is about an id we did not ask for, so the
                                # model in hand is innocent. Retiring it here is how a
                                # single malformed field condemned a whole pool one
                                # blameless model at a time.
                                log.error(
                                    "%s rejected %r, which is not the model asked for (%s); "
                                    "leaving it in the pool: %s",
                                    provider.name, blamed, model, body[:200],
                                )
                                return ChatResult(
                                    error=f"HTTP 400: {provider.name} rejected {blamed}",
                                    error_kind="rejected",
                                )
                            # A durable fact about (service, model), not about this
                            # turn: OpenRouter was asked for a Google-shaped id seven
                            # times in forty-five seconds and rejected every one,
                            # because nothing remembered the first refusal.
                            self._unknown.add((provider.name, model))
                            # In the service's own words. This was read into `body` and
                            # thrown away, so a log full of "does not serve" never said
                            # whether the models were missing or the account was.
                            log.warning(
                                "%s does not serve %s; it will not be asked again: %s",
                                provider.name, model, body[:200],
                            )
                            if self._disowns_everything(provider, body):
                                return ChatResult(
                                    error=f"{provider.name} refused every model it lists",
                                    error_kind="rejected",
                                )
                            alternative = self._next_model(
                                provider, requested, tried=tried, vision=vision, audio=audio
                            )
                            if alternative:
                                model = alternative
                                continue
                            return ChatResult(
                                error=f"HTTP 400: {provider.name} serves none of these models",
                                error_kind="rejected",
                            )
                        if vision and _about_images(lowered):
                            # Learned once and remembered for the life of the process,
                            # so every later photo skips this model instead of spending
                            # a call to be told the same thing. Its service keeps its
                            # other models: the two are not the same fact.
                            self._text_only.add((provider.name, model))
                            log.info("%s cannot take images on %s; it will be skipped "
                                     "for media from now on", provider.name, model)
                        log.error("%s rejected the request: %s", provider.name, body)
                        return ChatResult(error=f"HTTP 400: {body[:200]}", error_kind="rejected")

                    if resp.status_code == 402:
                        fault = self._note_fault(resp, provider, model)
                        if self._s.free_mode and provider.discovers_free_models:
                            # One model's free allowance is spent; the account is fine.
                            self._rest(model, fault.wait or QUOTA_COOLDOWN)
                            alternative = self._next_free(tried=tried, vision=vision, audio=audio)
                            if alternative:
                                log.info("%s is out of free quota, switching to %s",
                                         model, alternative)
                                model = alternative
                                continue
                            log.error("every free model is out of quota for now")
                            return ChatResult(error=fault.summary, error_kind="payment")
                        # On paid models this is terminal: retrying cannot conjure credit.
                        self._pause_provider(provider, fault.wait or QUOTA_COOLDOWN, fault.summary)
                        return ChatResult(error=fault.summary, error_kind="payment")

                    if resp.status_code in (401, 403):
                        # 401 is a claim about the key; 403 is a claim about this
                        # request, and a 403 that never reached the auth layer is an
                        # edge block rather than a bad key - which the reader says.
                        fault = self._note_fault(resp, provider, model)
                        refused = fault.kind == faults.AUTH
                        detail = fault.summary
                        log.error(
                            "%s turned a request away%s",
                            provider.name,
                            f" [{credential.label}]" if credential.label else "",
                        )
                        self._rest_credential(
                            credential, AUTH_COOLDOWN if refused else FORBIDDEN_COOLDOWN, detail
                        )

                        spare = provider.next_key(credential, time.time())
                        if spare is not None:
                            log.info("%s: trying the next key", provider.name)
                            credential = spare
                            continue

                        if len(self.providers) > 1:
                            # With nothing else to fall back on, keep trying: a lone
                            # service silenced for a day is worse than a wasted call.
                            #
                            # And the same distinction the credential gets, because it
                            # is the same claim: a 401 is about the key and lasts, a
                            # 403 is about this request and usually does not. Resting
                            # the key ten minutes and the service twenty-four hours on
                            # one 403 is what benched OpenRouter for a day twenty-four
                            # minutes after it last answered.
                            self._pause_provider(
                                provider,
                                AUTH_COOLDOWN if refused else FORBIDDEN_COOLDOWN,
                                detail,
                            )
                        log.error("check %s or set a key from the panel", provider.key_env)
                        # Only a 401 is a claim about the key. A 403 that never
                        # reached the auth layer is an edge block, and calling it
                        # "auth" had the panel report "the key was refused" for a
                        # key that was working - which is a whole afternoon spent
                        # replacing the wrong thing.
                        return ChatResult(
                            error=detail, error_kind="auth" if refused else "blocked"
                        )

                    if resp.status_code == 404:
                        # Model ids differ between services and change over time, so
                        # the list is walked before the service is written off.
                        body = resp.text[:300]
                        log.error("%s does not serve %s: %s", provider.name, model, body)
                        alternative = self._next_model(
                            provider, requested, tried=tried, vision=vision, audio=audio
                        )
                        if alternative:
                            model = alternative
                            continue
                        return ChatResult(
                            error=f"HTTP 404: {provider.name} serves none of the configured models",
                            error_kind="rejected",
                        )

                    if resp.status_code >= 400:
                        # The status alone never says which field or model the service
                        # objected to; the reader keeps what it did say.
                        fault = self._note_fault(resp, provider, model)
                        return ChatResult(error=fault.summary, error_kind="rejected")

                    data = resp.json()

                    if data.get("error") and not data.get("choices"):
                        last_error = str(data["error"].get("message", data["error"]))[:200]
                        log.warning("provider error: %s", last_error)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 20.0)
                        continue

                    result = self._parse(data)
                    if result.ok:
                        result.service = provider.name
                        # A bare service often echoes no model name, and the one asked
                        # for is not the one that ran once free mode has walked on.
                        result.model = result.model or model
                        result.latency_ms = int((time.monotonic() - started) * 1000)
                        self._note_result(provider, credential, result)
                        self._scores[model] = min(self._scores.get(model, 0) + 1, 3)
                        if self._s.free_mode and attempt > 1:
                            log.info("answered by %s after %d attempts", model, attempt)
                        return result

                    last_error = result.error or "empty completion"
                    log.warning("empty completion from %s (attempt %d)", model, attempt)

                    # Some models answer an unsupported parameter with silence rather
                    # than an error, so drop the optional one before blaming the model.
                    if reasoning:
                        log.info("retrying %s without reasoning parameters", model)
                        reasoning = None
                        continue

                    # A model that keeps returning nothing is unusable for this turn,
                    # and waiting will not change that: move to the next one.
                    if self._s.free_mode and provider.discovers_free_models:
                        self.mark_unusable(model)
                        alternative = self._next_free(tried=tried, vision=vision, audio=audio)
                        if alternative:
                            log.info("%s returned nothing, switching to %s", model, alternative)
                            model = alternative
                            continue

                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 20.0)

                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"network: {exc}"
                    log.warning("network error (attempt %d/%d): %s", attempt, retries, exc)
                    # noqa below: retry jitter, nothing to guess
                    await asyncio.sleep(delay + random.uniform(0, 0.8))  # noqa: S311
                    delay = min(delay * 2, 20.0)
                except Exception as exc:
                    log.exception("unexpected error calling the model")
                    return ChatResult(error=str(exc))

            log.error("no completion after %d attempts (%s)", retries, last_error)
            return ChatResult(error=last_error)

    async def json_chat(
        self,
        messages: list[dict],
        *,
        model: str,
        max_tokens: int = 120,
        temperature: float = 0.0,
    ) -> tuple[dict | None, Usage]:
        result = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            max_retries=2,
        )
        if not result.ok:
            return None, result.usage
        return parse_json(result.text or ""), result.usage


# A service saying it does not have this model at all, in the words each of them
# uses. Distinct from a 400 about the request's shape, which is this turn's
# problem rather than the model's.
_NOT_A_MODEL = (
    "not a valid model", "is not a valid model id", "model not found",
    "no such model", "unknown model", "invalid model", "does not exist",
    "model_not_found",
)


def _about_the_model(body: str) -> bool:
    return any(phrase in body for phrase in _NOT_A_MODEL)


# The id a refusal is actually about. Services quote it back, in a few shapes:
#   "models/gemini-2.5-flash is not a valid model ID"
#   "`meta/llama-4` is not a valid model"
#   "model not found: some/thing"
_BLAMED = re.compile(
    r"[\"'`]?([\w./:-]{3,80})[\"'`]?\s+is\s+not\s+a\s+valid\s+model"
    r"|model\s+not\s+found:?\s+[\"'`]?([\w./:-]{3,80})",
    re.I,
)


def _names_another_model(body: str, asked: str) -> str:
    """The id a refusal blames, when that is not the one we asked for.

    OpenRouter takes a list of alternatives alongside the model, so a refusal can
    be about an entry in that list rather than about the model itself - and
    reading it as the latter is how one bad entry retired every model in a pool.
    """
    match = _BLAMED.search(body or "")
    if not match:
        return ""
    blamed = (match.group(1) or match.group(2) or "").strip("\"'`")
    return blamed if blamed and blamed != (asked or "") else ""


def _about_images(body: str) -> bool:
    """Whether a 400 is the service saying it cannot read pictures at all.

    Seen in the wild as "image content is not supported for this model" and as a
    bare input-validation error naming the image part.
    """
    return any(
        word in body
        for word in ("image", "vision", "multimodal", "image_url", "not supported for this model")
    )


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("retry-after")
    try:
        return min(float(raw), 20.0) if raw else default
    except (TypeError, ValueError):
        return default


def parse_json(text: str) -> dict | None:
    """Tolerant JSON extraction: handles code fences and surrounding prose."""
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
