"""OpenRouter chat client: retries, model fallbacks, reasoning control, web search."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import catalog
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
QUOTA_COOLDOWN = 6 * 3600.0
# A refused key is a configuration problem; asking again this run cannot fix it.
AUTH_COOLDOWN = 24 * 3600.0

# A free-tier 429 is account-wide, so every model is equally unavailable and the
# only useful response is for the whole bot to stop knocking for a while.
ACCOUNT_PAUSE = 60.0


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
        self._scores: dict[str, int] = {}
        self._pace_lock = asyncio.Lock()

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
        self._registry.record_call(
            provider.name, tokens=result.usage.total_tokens, cost=result.usage.cost
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
            provider.paused_until = max(was_paused, provider.paused_until)

        if result.ok:
            return True, f"answered by {result.model}"
        if result.error_kind == "auth":
            return False, "the key was refused"
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
        async with self._pace_lock:
            wait = provider.last_request + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            provider.last_request = time.monotonic()

    # -- model catalog ---------------------------------------------------
    async def _confirm_models(self, provider: providers_mod.Provider) -> None:
        """Keep only the model ids this service actually lists.

        Preset ids go stale as services rename and retire models, and the answer
        to asking for one that is gone is a 404 per message. The service's own
        listing is the cheapest way to find out at startup instead.
        """
        try:
            credential = provider.pick(time.time())
            headers = self._auth(credential) if credential else {}
            resp = await self._clients[provider.name].get(
                provider.models_url, timeout=20.0, headers=headers
            )
            resp.raise_for_status()
            offered = {m["id"] for m in resp.json().get("data", []) if m.get("id")}
        except Exception as exc:
            log.info(
                "%s did not list its models (%s); using the configured ids", provider.name, exc
            )
            return
        if not offered:
            return

        # Services vary on whether ids carry a namespace prefix.
        def known(model: str) -> bool:
            return model in offered or f"models/{model}" in offered

        kept = [m for m in provider.models if known(m)]
        dropped = [m for m in provider.models if not known(m)]
        if dropped:
            log.warning("%s does not offer %s", provider.name, ", ".join(dropped))
        if not kept:
            log.warning(
                "%s offers none of the configured models; set %s_MODELS from: %s",
                provider.name,
                provider.name.upper(),
                ", ".join(sorted(offered)[:12]),
            )
            return
        provider.models = kept
        provider.vision_models = [m for m in provider.vision_models if known(m)]
        log.info("%s: %s", provider.name, ", ".join(kept))

    async def load_catalog(self) -> None:
        for provider in self.providers:
            if not provider.discovers_free_models:
                await self._confirm_models(provider)

        discovering = next((p for p in self.providers if p.discovers_free_models), None)
        if discovering is None:
            log.info("no provider advertises a free catalog; using configured models")
            self._seed_free_models()
            return
        client = self._clients[discovering.name]
        try:
            credential = discovering.pick(time.time())
            resp = await client.get(
                discovering.models_url,
                timeout=25.0,
                headers=self._auth(credential) if credential else {},
            )
            resp.raise_for_status()
            entries = resp.json().get("data", [])
        except Exception as exc:
            log.warning("could not load model catalog (%s); skipping validation", exc)
            self._catalog = None
            self._seed_free_models()
            return

        self._catalog = {m["id"] for m in entries if m.get("id")}
        log.info("loaded model catalog (%d models)", len(self._catalog))
        self._index_free_models(entries)

    _is_free = staticmethod(catalog.is_free)
    _is_chat = staticmethod(catalog.is_chat)

    def context_window(self, model: str) -> int:
        """Tokens this model can hold, or 0 when the catalog never named it."""
        for entry in self._models:
            if entry.id == model:
                return entry.context
        return 0

    def models_offered(self, *, free_only: bool = True, vision: bool = False) -> list[Model]:
        """The chat models the catalog listed, for the panel to show and choose from."""
        return [
            m
            for m in self._models
            if (m.free or not free_only) and (m.vision or not vision)
        ]

    def _index_free_models(self, entries: list[dict]) -> None:
        """Discover zero-cost models instead of shipping a list that goes stale."""
        self._models = catalog.read(entries)
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

    def _all_free(self, *, vision: bool = False, audio: bool = False) -> list[str]:
        configured = list(self._s.free_models)
        if configured:
            return configured
        if audio:
            return list(self._free_audio)
        return list(self._free_vision if vision else self._free_text)

    def free_pool(self, *, vision: bool = False, audio: bool = False) -> list[str]:
        """Usable free models, worst-behaved last, skipping any that are resting."""
        candidates = self._all_free(vision=vision, audio=audio)
        now = time.monotonic()
        available = [m for m in candidates if self._cooldowns.get(m, 0.0) <= now]
        # If every one is resting, try them anyway rather than going silent.
        pool = available or candidates
        # Stable sort: models that have misbehaved sink, the rest keep catalog order.
        return sorted(pool, key=lambda m: -self._scores.get(m, 0))

    def supports_free_vision(self) -> bool:
        return bool(self._all_free(vision=True))

    def supports_free_audio(self) -> bool:
        return bool(self._all_free(audio=True))

    def _rest(self, model: str, seconds: float) -> None:
        self._cooldowns[model] = time.monotonic() + seconds

    def mark_unusable(self, model: str, seconds: float = EMPTY_COOLDOWN) -> None:
        """Retire a model the caller judged unusable, e.g. it wrote nonsense."""
        if model:
            self._rest(model, seconds)
            self._scores[model] = self._scores.get(model, 0) - 1
            log.info("resting %s: unusable reply", model)

    def _next_free(self, *, tried: set[str], vision: bool, audio: bool) -> str | None:
        for candidate in self.free_pool(vision=vision, audio=audio) or self.free_pool():
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
            return self.free_pool(vision=vision, audio=audio) or self.free_pool() or [requested]
        options = provider.vision_models if vision else provider.models
        return list(options or provider.models or [requested])

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
        if provider.discovers_free_models:
            return self.resolve(requested, vision=vision, audio=audio)
        return self._candidates(provider, requested, vision=vision, audio=audio)[0]

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
            return self._next_free(tried=tried, vision=vision, audio=audio)
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
            alternatives = self.free_pool() if self._s.free_mode else self._s.fallback_models
            if alternatives:
                chain = [model] + [m for m in alternatives if m != model]
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
            waits = [p.paused_until - time.monotonic() for p in self.providers]
            return ChatResult(
                error=f"every provider is resting for another {max(waits):.0f}s",
                error_kind="throttled",
            )

        last = ChatResult(error="no provider attempted", error_kind="throttled")
        for provider in live:
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
                    self._pause_provider(provider, ACCOUNT_PAUSE)
                continue  # a spent service says nothing about the next one
            if last.error_kind in ("rejected", "auth") and len(live) > 1:
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
                resp = await client.post(
                    provider.chat_url, json=payload, headers=self._auth(credential)
                )

                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                    if self._s.free_mode and resp.status_code == 429:
                        # The allowance belongs to this account, so every model
                        # behind it is equally limited; the caller moves on to the
                        # next service rather than touring this one's models.
                        self._pause_provider(provider, _retry_after(resp, ACCOUNT_PAUSE))
                        return ChatResult(
                            error=f"HTTP 429: {provider.name} limit", error_kind="throttled"
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
                        log.warning("model %s rejected reasoning params, retrying without", model)
                        reasoning = None
                        continue
                    if web and ("plugin" in lowered or "web" in lowered):
                        log.warning("web plugin rejected, retrying without search")
                        web = False
                        continue
                    log.error("%s rejected the request: %s", provider.name, body)
                    return ChatResult(error=f"HTTP 400: {body[:200]}", error_kind="rejected")

                if resp.status_code == 402:
                    if self._s.free_mode and provider.discovers_free_models:
                        # One model's free allowance is spent; the account is fine.
                        self._rest(model, QUOTA_COOLDOWN)
                        alternative = self._next_free(tried=tried, vision=vision, audio=audio)
                        if alternative:
                            log.info("%s is out of free quota, switching to %s",
                                     model, alternative)
                            model = alternative
                            continue
                        log.error("every free model is out of quota for now")
                        return ChatResult(
                            error="HTTP 402: free quota exhausted", error_kind="payment"
                        )
                    # On paid models this is terminal: retrying cannot conjure credit.
                    log.error(
                        "out of credit (HTTP 402) - top up at https://openrouter.ai/credits"
                    )
                    return ChatResult(error="HTTP 402: out of credit", error_kind="payment")

                if resp.status_code in (401, 403):
                    detail = f"HTTP {resp.status_code}: the key was refused"
                    log.error(
                        "%s refused a key (%s)%s",
                        provider.name,
                        resp.status_code,
                        f" [{credential.label}]" if credential.label else "",
                    )
                    self._rest_credential(credential, AUTH_COOLDOWN, detail)

                    spare = provider.next_key(credential, time.time())
                    if spare is not None:
                        log.info("%s: trying the next key", provider.name)
                        credential = spare
                        continue

                    if len(self.providers) > 1:
                        # With nothing else to fall back on, keep trying: a lone
                        # service silenced for a day is worse than a wasted call.
                        self._pause_provider(provider, AUTH_COOLDOWN, detail)
                    log.error("check %s or set a key from the panel", provider.key_env)
                    return ChatResult(error=detail, error_kind="auth")

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
                    # Log the body: the status alone never says which field or
                    # model the service objected to.
                    body = resp.text[:300]
                    log.error("%s returned HTTP %s: %s", provider.name, resp.status_code, body)
                    return ChatResult(
                        error=f"HTTP {resp.status_code}: {body[:200]}", error_kind="rejected"
                    )

                data = resp.json()

                if data.get("error") and not data.get("choices"):
                    last_error = str(data["error"].get("message", data["error"]))[:200]
                    log.warning("provider error: %s", last_error)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 20.0)
                    continue

                result = self._parse(data)
                if result.ok:
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
                    self._rest(model, EMPTY_COOLDOWN)
                    self._scores[model] = self._scores.get(model, 0) - 1
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
