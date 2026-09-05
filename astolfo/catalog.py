"""What a service offers, kept as data instead of a list of ids.

`llm.py` only ever needed the ids: which model to call, and which of them cost
nothing. The panel needs more than that to be useful - a readable name, the
context window, whether it can read a picture, and what it charges - so the
catalog is parsed once into these records and everything else reads them.

Free models come and go weekly. Nothing here is hardcoded: it is whatever the
service listed the last time the catalog was loaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Not a conversation partner: safety filters, embedders, and the speech and
# image models the bare listings sit right next to the chat ones.
# Things a service lists that you cannot hold a conversation with. This is the
# only filter a bare listing gets, so it has to cover the neighbours - and the
# neighbours are worse than they look. DeepInfra's own listing opens with four
# BAAI embedding models and four Bria image tools, and OpenRouter carries
# Google's computer-use preview, which took a real turn in the group before it
# came back as "not a valid model ID".
NOT_CONVERSATIONAL = (
    # judges and scorers
    "content-safety", "guard", "moderation", "classif", "detect",
    # vectors, not conversation
    "embed", "rerank", "bge-", "gte-", "e5-", "siglip", "clip-vit",
    # speech
    "whisper", "tts", "-stt", "speech", "transcribe", "voice-",
    # pictures
    "diffusion", "flux", "dall-e", "sdxl", "stable-", "upscal", "imagen",
    "nano-banana", "seedream", "inpaint", "outpaint", "background",
    "foreground", "segment", "bria", "-image", "to-image", "ocr",
    # moving pictures and sound
    # "/kling" rather than "kling": inkling-small is a real chat model, and a
    # bare substring quietly took it out of the pool.
    "veo-", "sora", "/kling", "runway", "seedance", "to-video", "lyria",
    "suno", "musicgen", "audiogen",
    # driving a machine rather than talking to one
    "computer-use", "computer_use",
)

# How much a service tells us about its models. Only OpenRouter answers in full;
# Groq adds the context window to the OpenAI shape; the rest give an id and
# nothing else, which is why everything below has to fall back to the name.
RICH = "openrouter"
SIZED = "groq"
BARE = "bare"

# Context windows by name, for the services that will not say. Longest key wins,
# so a specific model beats its family. These are the published windows; being
# wrong low costs a little history, being wrong high overflows, so where a family
# is uncertain the smaller number is used.
_WINDOWS: dict[str, int] = {
    "gpt-oss": 131072,
    "llama-4-scout": 131072,
    "llama-4-maverick": 131072,
    "llama-4": 131072,
    "llama-3.3": 131072,
    "llama-3.2": 131072,
    "llama-3.1": 131072,
    "llama-3": 8192,
    "qwen3-coder": 262144,
    "qwen3": 131072,
    "qwen2.5": 32768,
    "qwen2": 32768,
    "gemma-3": 131072,
    "gemma-2": 8192,
    "gemini-2.5-pro": 1048576,
    "gemini-2.5-flash": 1048576,
    "gemini-flash": 1048576,
    "gemini-pro": 1048576,
    "gemini": 1048576,
    "deepseek-reasoner": 65536,
    "deepseek-chat": 65536,
    "deepseek-v3": 65536,
    "deepseek": 65536,
    "mistral-small": 32768,
    "mistral-nemo": 131072,
    "open-mistral-nemo": 131072,
    "pixtral": 131072,
    "ministral": 131072,
    "command-r7b": 131072,
    "command-r": 131072,
    "command": 131072,
    "glm-5": 262144,
    "glm-4": 131072,
    "glm": 131072,
    "minimax": 1000000,
    "nemotron": 131072,
    "inkling": 1000000,
    "gpt-4o-mini": 128000,
    "gpt-4o": 128000,
    "gpt-4.1": 1047576,
    "gpt-5": 400000,
    "o4-mini": 200000,
    "phi-4": 16384,
    "smollm": 8192,
}
# When even the name says nothing. Small on purpose: a wrong-high guess overflows
# the window, a wrong-low one only shortens the history.
UNKNOWN_WINDOW = 8192

# Names that mean the model can read a picture, for listings with no modalities.
_SEES = re.compile(
    r"(vision|--?vl\b|[-_]vl[-_]|pixtral|llava|gemma-3|llama-4|gpt-4o|gpt-4\.1|gpt-5"
    r"|gemini|claude-3|inkling|qwen[\d.]*-?vl|internvl|molmo)",
    re.I,
)
_HEARS = re.compile(r"(audio|whisper|voxtral|omni|realtime)", re.I)


@dataclass(frozen=True)
class Model:
    """One model, as the service describes it - or as its name gives it away."""

    id: str
    name: str = ""
    context: int = 0
    free: bool = False
    vision: bool = False
    audio: bool = False
    prompt_price: float = 0.0
    completion_price: float = 0.0
    service: str = ""
    # True when the context window was read from the listing rather than guessed
    # from the id. A guess is still worth having - it is what stops the history
    # budget from being unbounded - but it is worth replacing the moment the
    # model tells us otherwise by refusing an overlong prompt.
    known: bool = True

    @property
    def short(self) -> str:
        """The id without its vendor prefix, which is what a button has room for."""
        return self.id.split("/", 1)[-1]

    @property
    def key(self) -> tuple[str, str]:
        return (self.service, self.id)

    @property
    def window(self) -> str:
        shown = f"{self.context // 1000}k" if self.context >= 1000 else str(self.context or "?")
        return shown if self.known else f"~{shown}"

    @property
    def marks(self) -> str:
        return ("🖼" if self.vision else "") + ("🎧" if self.audio else "")

    @property
    def price(self) -> str:
        """Per million tokens, which is how every service quotes it."""
        if self.free:
            return "free"
        million = 1_000_000
        return f"${self.prompt_price * million:.2f}/${self.completion_price * million:.2f} per M"


def _price(pricing: dict, key: str) -> float:
    try:
        return float(pricing.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_free(entry: dict, *, default: bool = False) -> bool:
    """Every priced dimension must be zero, not just tokens.

    Image, audio and per-request charges live in their own pricing keys, so
    checking prompt and completion alone marks paid models as free. This stays
    strict: it is guarding somebody's money.

    A listing with no pricing at all says nothing either way, and that is the
    shape every service but OpenRouter returns. `default` carries what the
    service is known for: on a free-tier service silence means free, anywhere
    else it means paid.
    """
    pricing = entry.get("pricing") or {}
    if not pricing:
        return default
    for value in pricing.values():
        if value in (None, ""):
            continue
        try:
            if float(value) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def is_chat(entry: dict) -> bool:
    """Whether this could hold a conversation.

    A rich listing says so outright. A bare one - which is what every service
    but OpenRouter returns - has only the id, and refusing those is why nothing
    outside OpenRouter was ever discovered. So the modality test applies when
    there are modalities to test, and the name carries it when there are not.
    """
    model_id = str(entry.get("id") or "").lower()
    if any(word in model_id for word in NOT_CONVERSATIONAL):
        return False

    architecture = entry.get("architecture") or {}
    inputs = architecture.get("input_modalities") or []
    outputs = architecture.get("output_modalities") or []
    if not inputs and not outputs:
        return True  # a bare listing; the name filter above is all there is

    # Where the listing does declare its modalities, they are trusted exactly as
    # strictly as before. A generator lists its real output alongside text - a
    # music model reports text+audio - so text has to be the whole of it.
    return "text" in inputs and set(outputs) == {"text"}


def window_for(model_id: str) -> int:
    """A context window guessed from the name, for listings that omit it."""
    lowered = (model_id or "").lower()
    best = ""
    for name in _WINDOWS:
        if name in lowered and len(name) > len(best):
            best = name
    return _WINDOWS[best] if best else UNKNOWN_WINDOW


def _context(entry: dict) -> int:
    """The window under any of the names the services give it."""
    for key in ("context_length", "context_window", "max_model_len", "max_input_tokens"):
        try:
            value = int(entry.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    top = entry.get("top_provider") or {}
    try:
        return int(top.get("context_length") or 0)
    except (TypeError, ValueError):
        return 0


def parse(entry: dict, *, service: str = "", free_tier: bool = False) -> Model | None:
    """One catalog entry as a record, or None when it cannot hold a conversation."""
    model_id = entry.get("id")
    if not model_id or not is_chat(entry):
        return None

    modalities = (entry.get("architecture") or {}).get("input_modalities") or []
    pricing = entry.get("pricing") or {}
    context = _context(entry)
    return Model(
        id=str(model_id),
        name=str(entry.get("name") or model_id),
        context=context or window_for(str(model_id)),
        free=is_free(entry, default=free_tier),
        vision="image" in modalities if modalities else bool(_SEES.search(str(model_id))),
        audio="audio" in modalities if modalities else bool(_HEARS.search(str(model_id))),
        prompt_price=_price(pricing, "prompt"),
        completion_price=_price(pricing, "completion"),
        service=service,
        known=bool(context),
    )


def read(entries: list[dict], *, service: str = "", free_tier: bool = False) -> list[Model]:
    """The chat models in a catalog listing, longest context first.

    Context order matters more than it looks: the persona prompt alone is a few
    thousand tokens, so a 4k model is not a candidate for anything.
    """
    parsed = (parse(entry, service=service, free_tier=free_tier) for entry in entries)
    return sorted((m for m in parsed if m), key=lambda m: (-m.context, m.id))


def search(models: list[Model], needle: str) -> list[Model]:
    words = [w for w in (needle or "").lower().split() if w]
    if not words:
        return list(models)
    return [m for m in models if all(w in f"{m.id} {m.name}".lower() for w in words)]
