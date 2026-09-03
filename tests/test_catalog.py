"""Reading a service's model listing into records the panel can show."""

from __future__ import annotations

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


def test_a_missing_context_length_does_not_break_the_listing() -> None:
    models = catalog.read([entry("x/y", context_length=None), entry("z/w", context_length="odd")])
    assert [m.context for m in models] == [0, 0]
    assert models[0].window == "?"


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
