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
import time
from dataclasses import dataclass, field


@dataclass
class Preset:
    name: str
    base_url: str
    key_env: str
    models: list[str] = field(default_factory=list)
    vision_models: list[str] = field(default_factory=list)
    discovers_free_models: bool = False
    # OpenRouter adds fields plain OpenAI-compatible services reject outright.
    openrouter_extensions: bool = False
    # Shown in the panel. "free tier" is the service's own offer, not a promise:
    # what it actually grants is what its test button reports.
    note: str = ""
    signup: str = ""


PRESETS: dict[str, Preset] = {
    "openrouter": Preset(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        discovers_free_models=True,
        openrouter_extensions=True,
    ),
    "google": Preset(
        name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GOOGLE_API_KEY",
        # The moving aliases, not pinned versions. Google retires a numbered model
        # "for new users" while still listing it, so a pinned id keeps working for
        # an old key and answers 404 to anyone who signed up after the cutoff -
        # which the model listing does not reveal, because it still names it.
        models=["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest"],
        vision_models=["gemini-flash-latest", "gemini-flash-lite-latest"],
        note="free tier",
        signup="aistudio.google.com/apikey",
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
        note="free tier",
        signup="cloud.cerebras.ai",
    ),
    "mistral": Preset(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        key_env="MISTRAL_API_KEY",
        models=["mistral-small-latest", "open-mistral-nemo"],
        vision_models=["pixtral-12b-latest"],
        note="free tier",
        signup="console.mistral.ai",
    ),
    "cohere": Preset(
        name="cohere",
        base_url="https://api.cohere.ai/compatibility/v1",
        key_env="COHERE_API_KEY",
        models=["command-r-08-2024", "command-r7b-12-2024"],
        note="trial keys are free and rate limited",
        signup="dashboard.cohere.com",
    ),
    "huggingface": Preset(
        name="huggingface",
        base_url="https://router.huggingface.co/v1",
        key_env="HUGGINGFACE_API_KEY",
        models=["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-7B-Instruct"],
        vision_models=["Qwen/Qwen2.5-VL-7B-Instruct"],
        note="monthly credit, then paid",
        signup="huggingface.co/settings/tokens",
    ),
    "sambanova": Preset(
        name="sambanova",
        base_url="https://api.sambanova.ai/v1",
        key_env="SAMBANOVA_API_KEY",
        models=["Meta-Llama-3.3-70B-Instruct", "Meta-Llama-3.1-8B-Instruct"],
        vision_models=["Llama-3.2-11B-Vision-Instruct"],
        note="free tier",
        signup="cloud.sambanova.ai",
    ),
    "deepinfra": Preset(
        name="deepinfra",
        base_url="https://api.deepinfra.com/v1/openai",
        key_env="DEEPINFRA_API_KEY",
        models=["meta-llama/Meta-Llama-3.1-8B-Instruct", "meta-llama/Llama-3.3-70B-Instruct"],
        vision_models=["meta-llama/Llama-3.2-11B-Vision-Instruct"],
        note="pay as you go after the signup credit",
        signup="deepinfra.com/dash/api_keys",
    ),
    "deepseek": Preset(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        key_env="DEEPSEEK_API_KEY",
        # Both are moving aliases DeepSeek keeps pointed at the current version.
        models=["deepseek-chat", "deepseek-reasoner"],
        note="pay as you go, cheap",
        signup="platform.deepseek.com/api_keys",
    ),
    "openai": Preset(
        name="openai",
        base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        models=["gpt-4o-mini", "gpt-4o"],
        vision_models=["gpt-4o-mini", "gpt-4o"],
        note="pay as you go",
        signup="platform.openai.com/api-keys",
    ),
    "aimlapi": Preset(
        name="aimlapi",
        base_url="https://api.aimlapi.com/v1",
        key_env="AIMLAPI_API_KEY",
        models=["gpt-4o-mini"],
        vision_models=["gpt-4o-mini"],
        note="small free allowance, then paid",
        signup="aimlapi.com/app/keys",
    ),
}


@dataclass
class Credential:
    """One key for one service, with the state that belongs to the key itself.

    More than one is for keys you already hold - replacing one without a gap,
    or a work key beside a personal one. Holding several accounts at one service
    to get past its free quota is a different thing: it breaks their terms and
    ends with all of them closed.
    """

    value: str
    id: int | None = None  # the database row, when it came from there
    label: str = ""
    enabled: bool = True
    rested_until: float = 0.0  # wall clock, so a rest outlives a restart
    last_error: str = ""

    def usable(self, now: float) -> bool:
        return self.enabled and bool(self.value) and self.rested_until <= now


@dataclass
class Provider:
    name: str
    base_url: str
    credentials: list[Credential] = field(default_factory=list)
    key_env: str = ""
    models: list[str] = field(default_factory=list)
    vision_models: list[str] = field(default_factory=list)
    discovers_free_models: bool = False
    openrouter_extensions: bool = False
    custom: bool = False
    paused_until: float = 0.0
    last_request: float = 0.0  # each service has its own per-minute allowance

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    @property
    def api_key(self) -> str:
        """The key a request would use right now, empty when none is usable."""
        credential = self.pick(time.time())
        return credential.value if credential else ""

    def pick(self, now: float) -> Credential | None:
        return next((c for c in self.credentials if c.usable(now)), None)

    def next_key(self, after: Credential, now: float) -> Credential | None:
        """Another key at this service, for when the current one is refused."""
        return next(
            (c for c in self.credentials if c is not after and c.usable(now)), None
        )


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


def discover(
    order: list[str],
    *,
    fallback_key: str = "",
    stored: list[dict] | None = None,
) -> list[Provider]:
    """Build the services to draw on, in the order they should be tried.

    Three sources, in increasing priority: the presets in this file, the
    environment, and what the owner has saved from the panel. A service is used
    only when it ends up with at least one usable key, so an install that has
    never opened the panel behaves exactly as it did before.
    """
    saved = {row["name"]: row for row in (stored or [])}
    names = list(dict.fromkeys([n.strip().lower() for n in order if n.strip()]))
    # A service added from the panel joins the list without editing PROVIDERS.
    names += [name for name in saved if name not in names]

    providers: list[Provider] = []
    for name in names:
        row = saved.get(name, {})
        if row.get("enabled") == 0:
            continue
        preset = PRESETS.get(name)
        if preset is None and not row.get("base_url"):
            continue  # neither the code nor the owner knows where to send this

        prefix = name.upper()
        base = preset.base_url if preset else ""
        keys = [
            Credential(
                value=str(credential["value"]),
                id=credential.get("id"),
                label=str(credential.get("label") or ""),
                enabled=bool(credential.get("enabled", 1)),
                rested_until=float(credential.get("rested_until") or 0.0),
                last_error=str(credential.get("last_error") or ""),
            )
            for credential in row.get("credentials", [])
            if credential.get("value")
        ]
        from_env = _env(preset.key_env) if preset else ""
        if not from_env and name == "openrouter":
            from_env = fallback_key
        if from_env:
            keys.append(Credential(value=from_env, label="from .env"))
        if not keys:
            continue

        providers.append(
            Provider(
                name=name,
                base_url=(
                    str(row.get("base_url") or "") or _env(f"{prefix}_BASE_URL", base)
                ),
                credentials=keys,
                key_env=preset.key_env if preset else f"{prefix}_API_KEY",
                models=(
                    _split(row.get("models"))
                    or _env_list(f"{prefix}_MODELS")
                    or list(preset.models if preset else [])
                ),
                vision_models=(
                    _split(row.get("vision_models"))
                    or _env_list(f"{prefix}_VISION_MODELS")
                    or list(preset.vision_models if preset else [])
                ),
                discovers_free_models=bool(
                    preset.discovers_free_models if preset else row.get("discovers_free_models")
                ),
                openrouter_extensions=bool(
                    preset.openrouter_extensions if preset else row.get("openrouter_extensions")
                ),
                custom=preset is None,
                paused_until=0.0,
            )
        )
    return providers


def _split(raw: object) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def unknown_names(order: list[str], stored: list[dict] | None = None) -> list[str]:
    known = set(PRESETS) | {row["name"] for row in (stored or [])}
    return [n for n in order if n.strip() and n.strip().lower() not in known]
