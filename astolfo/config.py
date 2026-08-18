"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _env(name: str, **kwargs: Any):
    """Declare a settings field bound to an environment variable."""
    metadata = {"env": name}
    if "default_factory" in kwargs:
        return field(default_factory=kwargs["default_factory"], metadata=metadata)
    return field(default=kwargs["default"], metadata=metadata)


def _coerce(annotation: str, raw: str) -> Any:
    value = raw.strip()
    if annotation.startswith("bool"):
        return value.lower() in {"1", "true", "yes", "on", "y"}
    if annotation.startswith("int"):
        return int(float(value))
    if annotation.startswith("float"):
        return float(value)
    lowered = annotation.lower().replace(" ", "")
    if "list[str]" in lowered:
        return [item.strip() for item in value.split(",") if item.strip()]
    if "list[int]" in lowered:
        numbers = []
        for item in value.split(","):
            try:
                numbers.append(int(item.strip()))
            except ValueError:
                continue
        return numbers
    return value or None if "none" in lowered else value


class ConfigError(RuntimeError):
    pass


# Every entry removes model calls per message, because free models are rationed by
# request count. Web search is off because the search plugin is billed even when
# the model itself is free.
FREE_MODE_PRESET = {
    "web_search": False,
    "router_llm": False,
    "summaries": False,
    "group_reply_chance": 0.12,
    "reply_cooldown": 45.0,
    "response_cache_ttl": 1800.0,
    "max_retries": 2,
    # Free vision models take images but are small; fewer, smaller frames keep a
    # GIF within what they will accept.
    "video_frames": 2,
    "image_max_dim": 768,
}


@dataclass(frozen=True)
class Settings:
    """All tunables. Every field maps to an environment variable."""

    # --- credentials -------------------------------------------------
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN", default="")
    api_key: str = _env("OPENROUTER_API_KEY", default="")
    api_base: str = _env("OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1")

    # --- models ------------------------------------------------------
    model_fast: str = _env("MODEL_FAST", default="google/gemini-2.5-flash")
    model_think: str = _env("MODEL_THINK", default="google/gemini-2.5-pro")
    model_search: str = _env("MODEL_SEARCH", default="google/gemini-2.5-flash")
    model_media: str = _env("MODEL_MEDIA", default="google/gemini-2.5-flash")
    model_router: str = _env("MODEL_ROUTER", default="google/gemini-2.5-flash-lite")
    model_summary: str = _env("MODEL_SUMMARY", default="google/gemini-2.5-flash-lite")
    fallback_models: list[str] = _env(
        "FALLBACK_MODELS",
        default_factory=lambda: ["openai/gpt-4o-mini", "anthropic/claude-3.5-haiku"],
    )
    provider_sort: str | None = _env("PROVIDER_SORT", default=None)  # price | throughput

    # Free mode: run on OpenRouter's zero-cost models. The limit there is requests
    # per minute and per day rather than tokens, so anything that spends an extra
    # request per message is switched off by the preset below.
    free_mode: bool = _env("FREE_MODE", default=False)
    free_models: list[str] = _env("FREE_MODELS", default_factory=list)
    # Free-tier limits are counted per account, not per model, so the whole bot
    # shares one budget of requests per minute no matter how many chats it serves.
    free_rpm: int = _env("FREE_RPM", default=8)

    # --- generation --------------------------------------------------
    temperature_fast: float = _env("TEMPERATURE_FAST", default=0.95)
    temperature_think: float = _env("TEMPERATURE_THINK", default=0.55)
    temperature_grounded: float = _env("TEMPERATURE_GROUNDED", default=0.25)
    max_tokens_fast: int = _env("MAX_TOKENS_FAST", default=260)
    max_tokens_think: int = _env("MAX_TOKENS_THINK", default=900)
    think_effort: str = _env("THINK_EFFORT", default="medium")  # low | medium | high
    fast_reasoning_budget: int = _env("FAST_REASONING_BUDGET", default=0)

    # --- retrieval ---------------------------------------------------
    web_search: bool = _env("WEB_SEARCH", default=True)
    web_max_results: int = _env("WEB_MAX_RESULTS", default=4)
    show_sources: bool = _env("SHOW_SOURCES", default=True)

    # --- routing -----------------------------------------------------
    router_llm: bool = _env("ROUTER_LLM", default=True)
    router_max_tokens: int = _env("ROUTER_MAX_TOKENS", default=80)
    router_min_words: int = _env("ROUTER_MIN_WORDS", default=4)

    # --- chat behaviour ----------------------------------------------
    group_reply_chance: float = _env("GROUP_REPLY_CHANCE", default=0.30)
    media_reply_chance: float = _env("MEDIA_REPLY_CHANCE", default=0.75)
    reply_cooldown: float = _env("REPLY_COOLDOWN", default=20.0)
    max_history: int = _env("MAX_HISTORY", default=80)
    history_char_budget: int = _env("HISTORY_CHAR_BUDGET", default=9000)
    max_input_chars: int = _env("MAX_INPUT_CHARS", default=1200)
    persona_reinject_every: int = _env("PERSONA_REINJECT", default=8)
    locale: str = _env("BOT_LANG", default="en")  # en | fa
    persona_locale: str = _env("PERSONA_LOCALE", default="auto")  # auto | en | fa

    # --- memory ------------------------------------------------------
    chat_ttl: float = _env("CHAT_TTL", default=12 * 3600)
    max_chats: int = _env("MAX_CHATS", default=800)
    summaries: bool = _env("SUMMARIES", default=True)
    data_dir: str = _env("DATA_DIR", default="data")

    # --- cost control ------------------------------------------------
    daily_budget_usd: float = _env("DAILY_BUDGET_USD", default=0.0)  # 0 = unlimited
    monthly_budget_usd: float = _env("MONTHLY_BUDGET_USD", default=0.0)
    chat_daily_call_limit: int = _env("CHAT_DAILY_CALL_LIMIT", default=0)
    response_cache: bool = _env("RESPONSE_CACHE", default=True)
    response_cache_ttl: float = _env("RESPONSE_CACHE_TTL", default=600.0)
    router_cache_ttl: float = _env("ROUTER_CACHE_TTL", default=3600.0)
    prompt_cache_control: bool = _env("PROMPT_CACHE_CONTROL", default=True)
    track_cost: bool = _env("TRACK_COST", default=True)

    # --- network -----------------------------------------------------
    request_timeout: float = _env("REQUEST_TIMEOUT", default=90.0)
    max_retries: int = _env("MAX_RETRIES", default=4)

    # --- media -------------------------------------------------------
    media_enabled: bool = _env("MEDIA_ENABLED", default=True)
    max_media_bytes: int = _env("MAX_MEDIA_BYTES", default=20 * 1024 * 1024)
    image_max_dim: int = _env("IMAGE_MAX_DIM", default=1024)
    image_quality: int = _env("IMAGE_QUALITY", default=82)
    video_frames: int = _env("VIDEO_FRAMES", default=4)
    max_audio_seconds: int = _env("MAX_AUDIO_SECONDS", default=240)

    # --- donations ---------------------------------------------------
    donate_enabled: bool = _env("DONATE", default=True)
    donate_amounts: list[int] = _env(
        "DONATE_AMOUNTS", default_factory=lambda: [15, 50, 150]
    )

    # --- runtime -----------------------------------------------------
    app_title: str = _env("APP_TITLE", default="Astolfo Telegram Bot")
    app_url: str = _env("APP_URL", default="https://github.com/hami9/Astolfo")
    keepalive: bool = _env("KEEPALIVE", default=True)
    keepalive_port: int = _env("PORT", default=8080)
    admin_ids: list[int] = _env("ADMIN_IDS", default_factory=list)
    log_level: str = _env("LOG_LEVEL", default="INFO")

    @classmethod
    def from_env(cls) -> Settings:
        values = {}
        for f in fields(cls):
            env_name = f.metadata.get("env")
            raw = os.getenv(env_name) if env_name else None
            if raw is None or not raw.strip():
                continue
            try:
                values[f.name] = _coerce(str(f.type), raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"invalid value for {env_name}: {raw!r} ({exc})") from exc

        if values.get("free_mode"):
            # Explicit environment variables still win over the preset.
            values = {**FREE_MODE_PRESET, **values}

        settings = cls(**values)
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", settings.telegram_token),
                ("OPENROUTER_API_KEY", settings.api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "missing required environment variables: "
                + ", ".join(missing)
                + " (set them in Replit Secrets or a local .env file)"
            )
        return settings

    @property
    def chat_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/models"

    def replace(self, **overrides: Any) -> Settings:
        """Return a copy with overrides applied (used by tests)."""
        return Settings(**{**self.__dict__, **overrides})
