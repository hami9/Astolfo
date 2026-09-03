"""What a service offers, kept as data instead of a list of ids.

`llm.py` only ever needed the ids: which model to call, and which of them cost
nothing. The panel needs more than that to be useful - a readable name, the
context window, whether it can read a picture, and what it charges - so the
catalog is parsed once into these records and everything else reads them.

Free models come and go weekly. Nothing here is hardcoded: it is whatever the
service listed the last time the catalog was loaded.
"""

from __future__ import annotations

from dataclasses import dataclass

# Text in, text out, but no conversation to be had.
NOT_CONVERSATIONAL = ("content-safety", "guard", "moderation", "embed", "rerank", "classif")


@dataclass(frozen=True)
class Model:
    """One model, as the service describes it."""

    id: str
    name: str = ""
    context: int = 0
    free: bool = False
    vision: bool = False
    audio: bool = False
    prompt_price: float = 0.0
    completion_price: float = 0.0

    @property
    def short(self) -> str:
        """The id without its vendor prefix, which is what a button has room for."""
        return self.id.split("/", 1)[-1]

    @property
    def window(self) -> str:
        if self.context >= 1000:
            return f"{self.context // 1000}k"
        return str(self.context) if self.context else "?"

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


def is_free(entry: dict) -> bool:
    """Every priced dimension must be zero, not just tokens.

    Image, audio and per-request charges live in their own pricing keys, so
    checking prompt and completion alone marks paid models as free.
    """
    pricing = entry.get("pricing") or {}
    if not pricing:
        return False
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
    """Take text in and give back text and nothing else.

    Generators list their extra output alongside text - a music model reports
    `text+image->text+audio` - so merely requiring text among the outputs
    still matches them. Only a model whose whole output is text can chat.
    """
    architecture = entry.get("architecture") or {}
    inputs = architecture.get("input_modalities") or []
    outputs = architecture.get("output_modalities") or []
    if "text" not in inputs or set(outputs) != {"text"}:
        return False
    return not any(word in (entry.get("id") or "").lower() for word in NOT_CONVERSATIONAL)


def parse(entry: dict) -> Model | None:
    """One catalog entry as a record, or None when it cannot hold a conversation."""
    model_id = entry.get("id")
    if not model_id or not is_chat(entry):
        return None
    modalities = (entry.get("architecture") or {}).get("input_modalities") or []
    pricing = entry.get("pricing") or {}
    try:
        context = int(entry.get("context_length") or 0)
    except (TypeError, ValueError):
        context = 0
    return Model(
        id=str(model_id),
        name=str(entry.get("name") or model_id),
        context=context,
        free=is_free(entry),
        vision="image" in modalities,
        audio="audio" in modalities,
        prompt_price=_price(pricing, "prompt"),
        completion_price=_price(pricing, "completion"),
    )


def read(entries: list[dict]) -> list[Model]:
    """The chat models in a catalog listing, longest context first.

    Context order matters more than it looks: the persona prompt alone is a few
    thousand tokens, so a 4k model is not a candidate for anything.
    """
    models = [model for model in (parse(entry) for entry in entries) if model]
    return sorted(models, key=lambda m: (-m.context, m.id))


def search(models: list[Model], needle: str) -> list[Model]:
    words = [w for w in (needle or "").lower().split() if w]
    if not words:
        return list(models)
    return [m for m in models if all(w in f"{m.id} {m.name}".lower() for w in words)]
