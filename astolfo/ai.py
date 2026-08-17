"""کلاینت OpenRouter: retry نمایی، مدل جایگزین، حالت تفکر، و جست‌وجوی وب."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings

log = logging.getLogger("astolfo.ai")


@dataclass
class Citation:
    title: str
    url: str


@dataclass
class ChatResult:
    text: Optional[str]
    model: str = ""
    citations: List[Citation] = field(default_factory=list)
    reasoning: Optional[str] = None
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.text)


class AIClient:
    """کلاینت async سازگار با OpenAI/OpenRouter."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=20.0),
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.app_url,
                "X-Title": settings.app_title,
            },
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=15),
        )
        self._available_models: Optional[set] = None
        self.total_tokens = 0
        self.total_calls = 0
        self.total_errors = 0

    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    async def load_model_catalog(self) -> None:
        """فهرست مدل‌های در دسترس را می‌گیرد تا شناسه‌های نامعتبر زود لو بروند."""
        try:
            resp = await self._client.get(self._s.models_url, timeout=25.0)
            resp.raise_for_status()
            data = resp.json().get("data") or []
            self._available_models = {m.get("id") for m in data if m.get("id")}
            log.info("فهرست مدل‌ها بارگذاری شد (%d مدل).", len(self._available_models))
        except Exception as exc:  # شکست اینجا نباید ربات را زمین بزند
            log.warning("فهرست مدل‌ها گرفته نشد (%s) — بدون اعتبارسنجی ادامه می‌دهیم.", exc)
            self._available_models = None

    def resolve_model(self, preferred: str) -> str:
        """اگر مدل خواسته‌شده موجود نبود، اولین جایگزین معتبر را برمی‌گرداند."""
        if not self._available_models or preferred in self._available_models:
            return preferred
        for candidate in self._s.fallback_models:
            if candidate in self._available_models:
                log.warning("مدل «%s» در دسترس نیست؛ «%s» جایگزین شد.", preferred, candidate)
                return candidate
        log.warning("هیچ جایگزینی برای «%s» پیدا نشد؛ همان ارسال می‌شود.", preferred)
        return preferred

    # ------------------------------------------------------------------
    def _build_payload(
        self,
        *,
        model: str,
        messages: List[dict],
        temperature: float,
        max_tokens: int,
        reasoning: Optional[dict],
        web: bool,
        response_format: Optional[dict],
        use_fallbacks: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if use_fallbacks and self._s.fallback_models:
            payload["models"] = [model] + [
                m for m in self._s.fallback_models if m != model
            ]
        if reasoning:
            payload["reasoning"] = reasoning
        if web and self._s.web_search_enabled:
            payload["plugins"] = [
                {"id": "web", "max_results": max(1, self._s.web_max_results)}
            ]
        if response_format:
            payload["response_format"] = response_format
        return payload

    @staticmethod
    def _extract(data: dict) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            return ChatResult(text=None, error="پاسخ بدون choice")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip() or None

        citations: List[Citation] = []
        for ann in message.get("annotations") or []:
            if ann.get("type") == "url_citation":
                cite = ann.get("url_citation") or {}
                url = cite.get("url")
                if url:
                    citations.append(Citation(title=(cite.get("title") or url)[:80], url=url))

        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            model=data.get("model", ""),
            citations=citations,
            reasoning=(message.get("reasoning") or None),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: List[dict],
        *,
        model: str,
        temperature: float = 0.8,
        max_tokens: int = 400,
        reasoning: Optional[dict] = None,
        web: bool = False,
        response_format: Optional[dict] = None,
        use_fallbacks: bool = True,
        max_retries: Optional[int] = None,
    ) -> ChatResult:
        model = self.resolve_model(model)
        retries = max_retries if max_retries is not None else self._s.max_retries
        delay = 1.5
        last_error = "نامشخص"

        for attempt in range(1, retries + 1):
            payload = self._build_payload(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning=reasoning,
                web=web,
                response_format=response_format,
                use_fallbacks=use_fallbacks,
            )
            try:
                self.total_calls += 1
                resp = await self._client.post(self._s.chat_url, json=payload)

                if resp.status_code in (429,) or resp.status_code >= 500:
                    wait = _retry_after(resp, delay)
                    last_error = f"HTTP {resp.status_code}"
                    log.warning(
                        "%s از سرویس (%s/%s) — %.1f ثانیه صبر", last_error, attempt, retries, wait
                    )
                    await asyncio.sleep(wait)
                    delay = min(delay * 2, 20.0)
                    continue

                if resp.status_code == 400:
                    body = resp.text[:600]
                    # برخی مدل‌ها پارامتر reasoning/plugins را نمی‌پذیرند
                    if reasoning and "reasoning" in body.lower():
                        log.warning("مدل %s پارامتر reasoning را نپذیرفت؛ بدون آن تلاش می‌کنیم.", model)
                        reasoning = None
                        continue
                    if web and ("plugin" in body.lower() or "web" in body.lower()):
                        log.warning("پلاگین وب پذیرفته نشد؛ بدون جست‌وجو تلاش می‌کنیم.")
                        web = False
                        continue
                    self.total_errors += 1
                    log.error("درخواست نامعتبر: %s", body)
                    return ChatResult(text=None, error=f"HTTP 400: {body[:200]}")

                if resp.status_code in (401, 403):
                    self.total_errors += 1
                    log.error("خطای احراز هویت (%s) — کلید API را بررسی کن.", resp.status_code)
                    return ChatResult(text=None, error=f"HTTP {resp.status_code}: کلید API نامعتبر")

                resp.raise_for_status()
                data = resp.json()

                if "error" in data and not data.get("choices"):
                    msg = str((data.get("error") or {}).get("message") or data["error"])
                    last_error = msg
                    log.warning("خطای سرویس: %s", msg[:200])
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 20.0)
                    continue

                result = self._extract(data)
                self.total_tokens += result.prompt_tokens + result.completion_tokens
                if not result.text:
                    last_error = result.error or "پاسخ خالی"
                    log.warning("پاسخ خالی از %s (تلاش %s)", model, attempt)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 20.0)
                    continue
                return result

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"شبکه: {exc}"
                log.warning("خطای شبکه (%s/%s): %s", attempt, retries, exc)
                await asyncio.sleep(delay + random.uniform(0, 0.8))
                delay = min(delay * 2, 20.0)
            except Exception as exc:  # pragma: no cover
                self.total_errors += 1
                log.exception("خطای غیرمنتظره در فراخوانی مدل")
                return ChatResult(text=None, error=str(exc))

        self.total_errors += 1
        log.error("پاسخ پس از %s تلاش دریافت نشد (%s).", retries, last_error)
        return ChatResult(text=None, error=last_error)

    # ------------------------------------------------------------------
    async def json_call(
        self,
        messages: List[dict],
        *,
        model: str,
        max_tokens: int = 120,
        temperature: float = 0.0,
    ) -> Optional[dict]:
        """فراخوانی کوتاه که خروجی JSON می‌خواهد (برای مسیریاب/خلاصه‌ساز)."""
        result = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            use_fallbacks=True,
            max_retries=2,
        )
        if not result.ok:
            return None
        return _loads_loose(result.text or "")


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("retry-after")
    try:
        return min(float(raw), 20.0) if raw else default
    except (TypeError, ValueError):
        return default


def _loads_loose(text: str) -> Optional[dict]:
    """JSON را حتی وقتی داخل ```json پیچیده شده باشد می‌خواند."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None
