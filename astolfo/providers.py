"""Several OpenAI-compatible services stacked behind one client.

Free allowances are counted per account, so one provider runs dry quickly. Every
service here grants its own quota, and using each within its own terms is simply
using the services; that is different from holding several accounts at one
provider to get around its limits, which their terms forbid and which usually
ends with all of them closed.

A preset only supplies the endpoint and sensible model names. Both are
overridable per provider, so a service changing its URL or model ids is a
configuration edit rather than a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Preset:
    name: str
    base_url: str
    key_env: str
    models: list[str] = field(default_factory=list)
    vision_models: list[str] = field(default_factory=list)
    discovers_free_models: bool = False


PRESETS: dict[str, Preset] = {
    "openrouter": Preset(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        discovers_free_models=True,
    ),
    "google": Preset(
        name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GOOGLE_API_KEY",
        models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        vision_models=["gemini-2.5-flash"],
    ),
    "groq": Preset(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        models=["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    ),
    "github": Preset(
        name="github",
        base_url="https://models.github.ai/inference",
        key_env="GITHUB_MODELS_TOKEN",
        models=["openai/gpt-4o-mini"],
        vision_models=["openai/gpt-4o-mini"],
    ),
    "cerebras": Preset(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        key_env="CEREBRAS_API_KEY",
        models=["llama-3.3-70b"],
    ),
}


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    vision_models: list[str] = field(default_factory=list)
    discovers_free_models: bool = False
    paused_until: float = 0.0
    last_request: float = 0.0  # each service has its own per-minute allowance

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


def discover(order: list[str], *, fallback_key: str = "") -> list[Provider]:
    """Build the providers named in `order` that actually have a key configured.

    Anything unknown is skipped with its name preserved in the log by the caller,
    so a typo does not silently become a missing provider.
    """
    providers: list[Provider] = []
    for raw in order:
        name = raw.strip().lower()
        preset = PRESETS.get(name)
        if preset is None:
            continue

        prefix = name.upper()
        key = _env(preset.key_env) or (fallback_key if name == "openrouter" else "")
        if not key:
            continue

        providers.append(
            Provider(
                name=name,
                base_url=_env(f"{prefix}_BASE_URL", preset.base_url),
                api_key=key,
                models=_env_list(f"{prefix}_MODELS") or list(preset.models),
                vision_models=(
                    _env_list(f"{prefix}_VISION_MODELS") or list(preset.vision_models)
                ),
                discovers_free_models=preset.discovers_free_models,
            )
        )
    return providers


def unknown_names(order: list[str]) -> list[str]:
    return [n for n in order if n.strip().lower() not in PRESETS]
