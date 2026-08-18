"""OpenRouter chat client: retries, model fallbacks, reasoning control, web search."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)


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
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._s = settings
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(settings.request_timeout, connect=20.0),
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.app_url,
                "X-Title": settings.app_title,
            },
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=15),
        )
        self._catalog: set | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- model catalog ---------------------------------------------------
    async def load_catalog(self) -> None:
        try:
            resp = await self._client.get(self._s.models_url, timeout=25.0)
            resp.raise_for_status()
            self._catalog = {m["id"] for m in resp.json().get("data", []) if m.get("id")}
            log.info("loaded model catalog (%d models)", len(self._catalog))
        except Exception as exc:
            log.warning("could not load model catalog (%s); skipping validation", exc)
            self._catalog = None

    def resolve(self, model: str) -> str:
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
        if fallbacks and self._s.fallback_models:
            payload["models"] = [model] + [m for m in self._s.fallback_models if m != model]
        if reasoning:
            payload["reasoning"] = reasoning
        if web and self._s.web_search:
            payload["plugins"] = [{"id": "web", "max_results": max(1, self._s.web_max_results)}]
        if response_format:
            payload["response_format"] = response_format
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
        model = self.resolve(model)
        retries = self._s.max_retries if max_retries is None else max_retries
        delay = 1.5
        last_error = "unknown"

        for attempt in range(1, retries + 1):
            payload = self._payload(
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
                resp = await self._client.post(self._s.chat_url, json=payload)

                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = _retry_after(resp, delay)
                    last_error = f"HTTP {resp.status_code}"
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
                    log.error("bad request: %s", body)
                    return ChatResult(error=f"HTTP 400: {body[:200]}")

                if resp.status_code == 402:
                    # Known and terminal: retrying cannot conjure credit.
                    log.error(
                        "out of credit (HTTP 402) - top up at https://openrouter.ai/credits"
                    )
                    return ChatResult(error="HTTP 402: out of credit", error_kind="payment")

                if resp.status_code in (401, 403):
                    log.error("auth failed (%s), check OPENROUTER_API_KEY", resp.status_code)
                    return ChatResult(
                        error=f"HTTP {resp.status_code}: invalid API key", error_kind="auth"
                    )

                resp.raise_for_status()
                data = resp.json()

                if data.get("error") and not data.get("choices"):
                    last_error = str(data["error"].get("message", data["error"]))[:200]
                    log.warning("provider error: %s", last_error)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 20.0)
                    continue

                result = self._parse(data)
                if result.ok:
                    return result
                last_error = result.error or "empty completion"
                log.warning("empty completion from %s (attempt %d)", model, attempt)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 20.0)

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"network: {exc}"
                log.warning("network error (attempt %d/%d): %s", attempt, retries, exc)
                await asyncio.sleep(delay + random.uniform(0, 0.8))
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
