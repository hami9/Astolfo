"""پیکربندی ربات آستولفو — همه‌چیز از متغیرهای محیطی (Replit Secrets) خوانده می‌شود."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:  # اختیاری: خواندن فایل .env در اجرای محلی
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - نبود dotenv نباید اجرا را متوقف کند
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name) or default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = _env(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # --- اعتبارنامه‌ها ---
    telegram_token: str
    api_key: str
    api_base: str = "https://openrouter.ai/api/v1"

    # --- مدل‌ها (شناسه‌های OpenRouter) ---
    model_fast: str = "google/gemini-2.5-flash"
    model_think: str = "google/gemini-2.5-pro"
    model_router: str = "google/gemini-2.5-flash-lite"
    model_media: str = "google/gemini-2.5-flash"
    model_search: str = "google/gemini-2.5-flash"
    model_summary: str = "google/gemini-2.5-flash-lite"
    fallback_models: List[str] = field(
        default_factory=lambda: ["openai/gpt-4o-mini", "anthropic/claude-3.5-haiku"]
    )

    # --- پارامترهای تولید ---
    temperature_fast: float = 0.95      # گپ‌وگفت بازیگوش
    temperature_think: float = 0.55     # وقتی دقت مهم است
    temperature_grounded: float = 0.25  # پاسخ مبتنی بر جست‌وجو
    max_tokens_fast: int = 260
    max_tokens_think: int = 900
    think_reasoning_effort: str = "medium"   # low | medium | high
    fast_reasoning_budget: int = 0           # 0 = خاموش‌کردن تفکر برای پاسخ سریع

    # --- جست‌وجوی وب (کاهش توهم) ---
    web_search_enabled: bool = True
    web_max_results: int = 4
    show_sources: bool = True

    # --- مسیریاب هوشمند ---
    router_llm_enabled: bool = True
    router_max_tokens: int = 80

    # --- رفتار گروه ---
    group_reply_chance: float = 0.30
    media_reply_chance: float = 0.75
    reply_cooldown_sec: float = 20.0
    max_history_len: int = 24
    max_chars_per_message: int = 1200
    persona_reinject_every: int = 8

    # --- حافظه ---
    chat_ttl_sec: float = 12 * 3600
    max_chats: int = 800
    summary_enabled: bool = True
    data_dir: str = "data"

    # --- شبکه ---
    request_timeout: float = 90.0
    max_retries: int = 4

    # --- رسانه ---
    media_enabled: bool = True
    max_media_bytes: int = 20 * 1024 * 1024   # سقف دانلود Bot API
    image_max_dim: int = 1152
    video_frames: int = 5
    max_audio_seconds: int = 240

    # --- متفرقه ---
    app_title: str = "Astolfo Telegram Bot"
    app_url: str = "https://github.com/hami9/astolfo"
    keepalive: bool = True
    keepalive_port: int = 8080
    admin_ids: List[int] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        token = _env("TELEGRAM_BOT_TOKEN")
        api_key = _env("OPENROUTER_API_KEY") or _env("AI_API_KEY")

        missing = [
            name
            for name, value in (("TELEGRAM_BOT_TOKEN", token), ("OPENROUTER_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise SystemExit(
                "متغیرهای محیطی زیر تعریف نشده‌اند: "
                + ", ".join(missing)
                + "\nدر Replit از بخش Secrets و در اجرای محلی از فایل .env استفاده کن."
            )

        admin_ids: List[int] = []
        for chunk in _env_list("ADMIN_IDS", []):
            try:
                admin_ids.append(int(chunk))
            except ValueError:
                continue

        return cls(
            telegram_token=token,
            api_key=api_key,
            api_base=_env("AI_API_BASE", cls.api_base).rstrip("/"),
            model_fast=_env("MODEL_FAST", cls.model_fast),
            model_think=_env("MODEL_THINK", cls.model_think),
            model_router=_env("MODEL_ROUTER", cls.model_router),
            model_media=_env("MODEL_MEDIA", cls.model_media),
            model_search=_env("MODEL_SEARCH", cls.model_search),
            model_summary=_env("MODEL_SUMMARY", cls.model_summary),
            fallback_models=_env_list(
                "FALLBACK_MODELS", ["openai/gpt-4o-mini", "anthropic/claude-3.5-haiku"]
            ),
            temperature_fast=_env_float("TEMPERATURE_FAST", cls.temperature_fast),
            temperature_think=_env_float("TEMPERATURE_THINK", cls.temperature_think),
            temperature_grounded=_env_float("TEMPERATURE_GROUNDED", cls.temperature_grounded),
            max_tokens_fast=_env_int("MAX_TOKENS_FAST", cls.max_tokens_fast),
            max_tokens_think=_env_int("MAX_TOKENS_THINK", cls.max_tokens_think),
            think_reasoning_effort=_env("THINK_EFFORT", cls.think_reasoning_effort),
            fast_reasoning_budget=_env_int("FAST_REASONING_BUDGET", cls.fast_reasoning_budget),
            web_search_enabled=_env_bool("WEB_SEARCH", cls.web_search_enabled),
            web_max_results=_env_int("WEB_MAX_RESULTS", cls.web_max_results),
            show_sources=_env_bool("SHOW_SOURCES", cls.show_sources),
            router_llm_enabled=_env_bool("ROUTER_LLM", cls.router_llm_enabled),
            group_reply_chance=_env_float("GROUP_REPLY_CHANCE", cls.group_reply_chance),
            media_reply_chance=_env_float("MEDIA_REPLY_CHANCE", cls.media_reply_chance),
            reply_cooldown_sec=_env_float("REPLY_COOLDOWN", cls.reply_cooldown_sec),
            max_history_len=_env_int("MAX_HISTORY", cls.max_history_len),
            persona_reinject_every=_env_int("PERSONA_REINJECT", cls.persona_reinject_every),
            chat_ttl_sec=_env_float("CHAT_TTL", cls.chat_ttl_sec),
            summary_enabled=_env_bool("SUMMARY_ENABLED", cls.summary_enabled),
            data_dir=_env("DATA_DIR", cls.data_dir),
            request_timeout=_env_float("REQUEST_TIMEOUT", cls.request_timeout),
            media_enabled=_env_bool("MEDIA_ENABLED", cls.media_enabled),
            image_max_dim=_env_int("IMAGE_MAX_DIM", cls.image_max_dim),
            video_frames=_env_int("VIDEO_FRAMES", cls.video_frames),
            keepalive=_env_bool("KEEPALIVE", cls.keepalive),
            keepalive_port=_env_int("PORT", cls.keepalive_port),
            admin_ids=admin_ids,
        )

    @property
    def chat_url(self) -> str:
        return f"{self.api_base}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.api_base}/models"
