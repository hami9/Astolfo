"""Reading a service's model listing into records the panel can show."""

from __future__ import annotations

import pytest

from astolfo import catalog


def entry(model_id: str, **overrides) -> dict:
    base = {
        "id": model_id,
        "name": model_id,
        "context_length": 128000,
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    base.update(overrides)
    return base


# -- what counts as a model you can talk to -------------------------------
def test_a_generator_that_also_writes_text_is_not_a_chat_model() -> None:
    music = entry(
        "google/lyria",
        architecture={
            "input_modalities": ["text"],
            "output_modalities": ["text", "audio"],
        },
    )
    assert catalog.parse(music) is None


def test_a_classifier_is_not_a_chat_model() -> None:
    assert catalog.parse(entry("nvidia/nemotron-content-safety:free")) is None
    assert catalog.parse(entry("openai/text-embedding-3")) is None


def test_a_model_that_cannot_read_text_is_not_a_chat_model() -> None:
    image_only = entry(
        "some/img",
        architecture={"input_modalities": ["image"], "output_modalities": ["text"]},
    )
    assert catalog.parse(image_only) is None


# -- free or not ----------------------------------------------------------
def test_a_charge_in_any_dimension_makes_a_model_paid() -> None:
    """Prompt and completion at zero is not enough: images bill separately."""
    model = catalog.parse(entry("x/y", pricing={"prompt": "0", "completion": "0", "image": "0.04"}))
    assert model is not None and not model.free


def test_a_model_with_no_pricing_at_all_is_not_assumed_free() -> None:
    model = catalog.parse(entry("x/y", pricing={}))
    assert model is not None and not model.free


def test_a_zero_in_every_dimension_is_free() -> None:
    model = catalog.parse(entry("x/y:free", pricing={"prompt": "0", "completion": "0", "web": "0"}))
    assert model is not None and model.free
    assert model.price == "free"


def test_a_price_is_quoted_per_million_tokens() -> None:
    model = catalog.parse(
        entry("x/y", pricing={"prompt": "0.000003", "completion": "0.000015"})
    )
    assert model is not None
    assert model.price == "$3.00/$15.00 per M"


# -- the modalities the panel marks ---------------------------------------
def test_vision_and_audio_inputs_are_recorded() -> None:
    model = catalog.parse(
        entry(
            "x/omni",
            architecture={
                "input_modalities": ["text", "image", "audio"],
                "output_modalities": ["text"],
            },
        )
    )
    assert model is not None and model.vision and model.audio
    assert "🖼" in model.marks and "🎧" in model.marks


# -- reading a whole listing ----------------------------------------------
def test_the_longest_context_comes_first() -> None:
    models = catalog.read(
        [
            entry("a/small", context_length=8000),
            entry("b/huge", context_length=1000000),
            entry("c/medium", context_length=128000),
        ]
    )
    assert [m.id for m in models] == ["b/huge", "c/medium", "a/small"]


def test_entries_that_cannot_chat_are_dropped() -> None:
    models = catalog.read(
        [
            entry("good/one"),
            entry("bad/embed"),
            {"name": "no id"},
        ]
    )
    assert [m.id for m in models] == ["good/one"]


def test_a_missing_context_length_is_guessed_from_the_name() -> None:
    """A guess is what keeps the history budget bounded; zero left it unbounded."""
    models = catalog.read([entry("x/qwen3-32b", context_length=None)])
    assert models[0].context == catalog.window_for("qwen3-32b")
    assert models[0].window.startswith("~"), "shown as a guess, not as fact"
    assert not models[0].known


def test_a_name_nobody_recognises_gets_the_cautious_window() -> None:
    """Guessing high overflows the model; guessing low only shortens the history."""
    model = catalog.parse(entry("some/thing-nobody-has-heard-of", context_length=None))
    assert model is not None and model.context == catalog.UNKNOWN_WINDOW


def test_the_window_is_written_the_way_people_say_it() -> None:
    assert catalog.parse(entry("x/y", context_length=128000)).window == "128k"
    assert catalog.parse(entry("x/y", context_length=900)).window == "900"


def test_the_short_name_drops_the_vendor_prefix() -> None:
    model = catalog.parse(entry("meta-llama/llama-3.3-70b-instruct:free"))
    assert model.short == "llama-3.3-70b-instruct:free"


# -- search ---------------------------------------------------------------
def test_search_matches_every_word_anywhere_in_the_id_or_name() -> None:
    models = catalog.read(
        [
            entry("meta-llama/llama-3.3-70b-instruct:free", name="Llama 3.3 70B"),
            entry("google/gemini-2.0-flash:free", name="Gemini 2.0 Flash"),
            entry("qwen/qwen-2.5-7b", name="Qwen 2.5"),
        ]
    )
    assert [m.short for m in catalog.search(models, "llama 70b")] == [
        "llama-3.3-70b-instruct:free"
    ]
    assert len(catalog.search(models, "")) == 3
    assert catalog.search(models, "nothing here") == []


# -- a listing that says almost nothing ------------------------------------
def test_a_bare_listing_is_read_instead_of_thrown_away() -> None:
    """Twelve of the thirteen services answer like this, and all of it was dropped."""
    models = catalog.read(
        [
            {"id": "llama-4-scout-17b-16e-instruct"},
            {"id": "qwen3-32b"},
            {"id": "openai/gpt-oss-120b"},
        ],
        service="groq",
        free_tier=True,
    )
    assert [m.id for m in models] == [
        "llama-4-scout-17b-16e-instruct",
        "openai/gpt-oss-120b",
        "qwen3-32b",
    ]
    assert all(m.free and m.service == "groq" and not m.known for m in models)


def test_a_bare_listing_still_drops_what_cannot_chat() -> None:
    """The name is the only filter left, so it has to cover the neighbours."""
    ids = [m.id for m in catalog.read(
        [
            {"id": "llama-guard-4-12b"},
            {"id": "whisper-large-v3"},
            {"id": "playai-tts"},
            {"id": "black-forest-labs/flux-schnell"},
            {"id": "qwen3-32b"},
        ],
        service="groq",
    )]
    assert ids == ["qwen3-32b"]


def test_vision_is_read_from_the_name_when_there_are_no_modalities() -> None:
    seen = {m.id: m.vision for m in catalog.read(
        [{"id": "llama-4-scout"}, {"id": "Qwen/Qwen2.5-VL-7B-Instruct"},
         {"id": "pixtral-12b-latest"}, {"id": "qwen3-32b"}],
        service="x",
    )}
    assert seen["llama-4-scout"] and seen["Qwen/Qwen2.5-VL-7B-Instruct"]
    assert seen["pixtral-12b-latest"]
    assert not seen["qwen3-32b"]


def test_the_window_is_taken_from_whatever_the_service_calls_it() -> None:
    """Groq says context_window, others say max_model_len; only OpenRouter says context_length."""
    for key in ("context_length", "context_window", "max_model_len", "max_input_tokens"):
        model = catalog.parse({"id": "x/y", key: 65536})
        assert model is not None and model.context == 65536 and model.known, key
    nested = catalog.parse({"id": "x/y", "top_provider": {"context_length": 32768}})
    assert nested is not None and nested.context == 32768


def test_silence_about_price_follows_the_service() -> None:
    """A bare listing quotes nothing; what that means depends on where it came from."""
    assert catalog.parse({"id": "x/y"}, free_tier=True).free
    assert not catalog.parse({"id": "x/y"}, free_tier=False).free


def test_a_specific_name_beats_its_family() -> None:
    assert catalog.window_for("qwen3-coder-480b") > catalog.window_for("qwen3-32b")
    assert catalog.window_for("llama-3-8b") < catalog.window_for("llama-3.3-70b")


# -- things you cannot hold a conversation with ---------------------------
# Every id below is real: from OpenRouter's 400 in the live log, and from
# DeepInfra's own listing as the bot printed it at startup.
NOT_CHAT = [
    "google/gemini-2.5-computer-use-preview-10-2025",
    "BAAI/bge-base-en-v1.5",
    "BAAI/bge-m3",
    "BAAI/bge-en-icl",
    "Bria/blur_background",
    "Bria/erase_foreground",
    "Bria/expand",
    "Audio8/Audio8-TTS-Preview-0.6b",
    "intfloat/e5-large-v2",
    "google/imagen-3",
    "openai/sora-2",
    "kwaivgi/kling-v2",
    "qwen/qwen-image-edit",
]

REAL_CHAT = [
    "meta-llama/Llama-3.3-70B-Instruct",
    "google/gemini-2.5-flash",
    "minimax/minimax-m3:free",
    "thinkingmachines/inkling-small:free",
    "thinkingmachines/inkling:free",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "llama-4-scout-17b-16e-instruct",
    "qwen3-32b",
    "openai/gpt-oss-120b",
    "gemma-3-27b-it",
    "Qwen/Qwen2.5-VL-7B-Instruct",
    "pixtral-12b-latest",
    "command-r7b-12-2024",
    "gpt-4o-mini",
]


@pytest.mark.parametrize("model_id", NOT_CHAT)
def test_a_bare_listing_drops_what_cannot_hold_a_conversation(model_id: str) -> None:
    """A computer-use model took a real turn in the group and came back as
    "not a valid model ID". DeepInfra's listing opens with four embedding models
    and four image tools, and adoption would have taken them as chat models."""
    assert catalog.parse({"id": model_id}) is None, model_id


@pytest.mark.parametrize("model_id", REAL_CHAT)
def test_the_filter_does_not_eat_real_models(model_id: str) -> None:
    """The other half of the job. A name filter that is too eager is worse than
    one that is too slack: it silently shrinks the pool and nothing says why."""
    assert catalog.parse({"id": model_id}) is not None, model_id


def test_a_substring_does_not_take_out_an_innocent_neighbour() -> None:
    """"kling" is inside "inkling", and inkling-small is one of the free models
    the bot actually runs on. Caught by testing both directions, not by review."""
    assert catalog.parse({"id": "kwaivgi/kling-v2"}) is None
    assert catalog.parse({"id": "thinkingmachines/inkling-small:free"}) is not None
