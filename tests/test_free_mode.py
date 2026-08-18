"""Free mode: run on OpenRouter's zero-cost models, discovered at startup."""

from __future__ import annotations

import json

import httpx

from astolfo import chat
from astolfo.config import Settings
from astolfo.llm import LLMClient
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update

CATALOG = {
    "data": [
        {
            # Real shape from the live catalog: a music generator that also emits
            # text, so "text in the outputs" is not enough to exclude it.
            "id": "google/lyria-3-pro-preview",
            "context_length": 1048576,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text", "audio"],
            },
        },
        {
            "id": "nvidia/nemotron-3.5-content-safety:free",
            "context_length": 900000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
            },
        },
        {
            "id": "free/omni-audio",
            "context_length": 90000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {
                "input_modalities": ["text", "audio"],
                "output_modalities": ["text"],
            },
        },
        {
            "id": "paid/big",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        },
        {
            "id": "free/text-small",
            "context_length": 8000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
        {
            "id": "free/text-large",
            "context_length": 128000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
        {
            "id": "some/image-generator",
            "context_length": 900000,
            "pricing": {"prompt": "0", "completion": "0", "image": "0.04"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
        },
        {
            "id": "free/vision",
            "context_length": 64000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        },
    ]
}


def _catalog_client(settings, catalog=CATALOG, chat_body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog)
        if chat_body is not None:
            chat_body.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "free/text-large",
                "choices": [{"message": {"content": "ehehe~"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0},
            },
        )

    return LLMClient(settings, transport=httpx.MockTransport(handler))


def free_settings(settings, **overrides) -> Settings:
    # Pacing off by default so tests are not throttled; exercised on its own below.
    return settings.replace(**{"free_mode": True, "free_rpm": 0, **overrides})


async def test_preset_switches_off_per_message_extras(monkeypatch):
    monkeypatch.setenv("FREE_MODE", "1")
    loaded = Settings.from_env()

    assert loaded.free_mode is True
    assert loaded.web_search is False, "the search plugin is billed even on free models"
    assert loaded.router_llm is False, "the dispatcher would spend an extra request"
    assert loaded.summaries is False
    assert loaded.group_reply_chance < 0.2
    assert loaded.video_frames <= 2


async def test_explicit_settings_still_beat_the_preset(monkeypatch):
    monkeypatch.setenv("FREE_MODE", "1")
    monkeypatch.setenv("WEB_SEARCH", "1")
    assert Settings.from_env().web_search is True


async def test_free_models_are_discovered_and_ranked(settings):
    client = _catalog_client(free_settings(settings))
    await client.load_catalog()

    assert client.free_pool() == [
        "free/text-large", "free/omni-audio", "free/vision", "free/text-small",
    ]
    assert client.free_pool(vision=True) == ["free/vision"]
    assert client.supports_free_vision() is True
    assert "paid/big" not in client.free_pool()
    await client.aclose()


async def test_every_request_is_redirected_to_a_free_model(settings):
    bodies: list[dict] = []
    client = _catalog_client(free_settings(settings), chat_body=bodies)
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "hi"}], model="google/gemini-2.5-pro")
    assert bodies[0]["model"] == "free/text-large"
    assert bodies[0]["models"][0] == "free/text-large"
    assert "paid/big" not in bodies[0]["models"], "a paid model must never be a fallback"
    await client.aclose()


async def test_images_go_to_a_free_vision_model(settings):
    bodies: list[dict] = []
    client = _catalog_client(free_settings(settings), chat_body=bodies)
    await client.load_catalog()

    await client.chat(
        [{"role": "user", "content": [{"type": "text", "text": "look"},
                                      {"type": "image_url", "image_url": {"url": "data:,"}}]}],
        model="google/gemini-2.5-flash",
    )
    assert bodies[0]["model"] == "free/vision"
    await client.aclose()


async def test_no_free_vision_model_means_no_vision_claim(settings):
    text_only = {"data": [m for m in CATALOG["data"] if m["id"] != "free/vision"]}
    client = _catalog_client(free_settings(settings), catalog=text_only)
    await client.load_catalog()

    assert client.supports_free_vision() is False
    await client.aclose()


async def test_configured_list_overrides_discovery(settings):
    client = _catalog_client(free_settings(settings).replace(free_models=["my/pick"]))
    await client.load_catalog()
    assert client.free_pool() == ["my/pick"]
    await client.aclose()


async def test_seed_list_used_when_the_catalog_is_unreachable(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    cfg = free_settings(settings).replace(free_models=["backup/model"])
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    await client.load_catalog()

    assert client.free_pool() == ["backup/model"]
    await client.aclose()


# -- pipeline behaviour ---------------------------------------------------
class _VisionLLM:
    def __init__(self, vision: bool):
        self.vision = vision
        self.calls: list[dict] = []

    def resolve(self, model, *, vision=False, audio=False):
        return model

    def supports_free_vision(self) -> bool:
        return self.vision

    async def chat(self, messages, **kwargs):
        from astolfo.llm import ChatResult, Usage

        self.calls.append({"messages": messages, **kwargs})
        return ChatResult(text="ooh", model="free/x", usage=Usage())

    async def json_chat(self, messages, **kwargs):
        from astolfo.llm import Usage

        return None, Usage()


def _gif_message():
    from types import SimpleNamespace

    message = FakeMessage(None, caption="astolfo look at this gif")
    message.animation = SimpleNamespace(
        file_id="g1", file_size=1234, duration=3, thumbnail=None
    )
    return message


async def test_gif_frames_survive_when_a_free_vision_model_exists(rt, monkeypatch):
    from astolfo import media as media_mod

    rt.settings = rt.settings.replace(free_mode=True)
    rt.llm = _VisionLLM(vision=True)

    async def fake_collect(bot, message, settings):
        return media_mod.MediaBundle(
            parts=[{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BB"}}],
            notes=["2 sampled frames from a GIF"],
            placeholder=media_mod.PLACEHOLDERS["animation"],
            kind="animation",
        )

    monkeypatch.setattr(media_mod, "collect", fake_collect)
    message = _gif_message()
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    content = rt.llm.calls[0]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert sum(1 for part in content if part["type"] == "image_url") == 2
    assert message.sent


async def test_media_is_dropped_honestly_without_a_vision_model(rt, monkeypatch):
    from astolfo import media as media_mod

    rt.settings = rt.settings.replace(free_mode=True)
    rt.llm = _VisionLLM(vision=False)

    async def fake_collect(bot, message, settings):
        return media_mod.MediaBundle(
            parts=[{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}],
            notes=["1 sampled frame from a GIF"],
            placeholder=media_mod.PLACEHOLDERS["animation"],
            kind="animation",
        )

    monkeypatch.setattr(media_mod, "collect", fake_collect)
    message = _gif_message()
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    content = rt.llm.calls[0]["messages"][-1]["content"]
    assert isinstance(content, str), "no image parts may be sent to a text-only model"
    assert "cannot see attachments" in content
    assert message.sent


def test_status_reports_the_pool(rt):
    from astolfo.commands import _billing_label

    assert _billing_label(rt) == "paid models"

    rt.settings = rt.settings.replace(free_mode=True)
    rt.llm.free_pool = lambda **kw: ["a", "b"]
    rt.llm.supports_free_vision = lambda: True
    assert _billing_label(rt) == "free models (2 available, with images)"


def test_locale_is_forgiving_but_not_silent(caplog):
    from astolfo.strings import Strings, normalize_locale

    assert normalize_locale("fa") == "fa"
    assert normalize_locale("fa en") == "fa", "a stray second word must not lose Persian"
    assert normalize_locale("  FA  ") == "fa"
    assert normalize_locale("fa_IR") == "fa"
    assert normalize_locale("") == "en"
    assert normalize_locale(None) == "en"

    with caplog.at_level("WARNING"):
        assert normalize_locale("klingon") == "en"
    assert "not a supported language" in caplog.text

    assert Strings("fa en").locale == "fa"


async def test_non_chat_models_are_never_selected(settings):
    """A token-free music generator is not the cheapest chat model going."""
    client = _catalog_client(free_settings(settings))
    await client.load_catalog()

    pool = client.free_pool()
    assert "google/lyria-3-pro-preview" not in pool, "audio output is not a chat model"
    assert "some/image-generator" not in pool, "image output is not a chat model"
    assert pool[0] == "free/text-large", "longest-context real chat model wins"
    await client.aclose()


async def test_a_model_priced_on_another_axis_is_not_free(settings):
    entry = {
        "id": "sneaky/model",
        "context_length": 500000,
        "pricing": {"prompt": "0", "completion": "0", "request": "0.01"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    client = _catalog_client(free_settings(settings), catalog={"data": [entry, *CATALOG["data"]]})
    await client.load_catalog()

    assert "sneaky/model" not in client.free_pool(), "per-request charges are still charges"
    await client.aclose()


# -- selection against real catalog shapes --------------------------------
async def test_a_generator_that_also_emits_text_is_excluded(settings):
    client = _catalog_client(free_settings(settings))
    await client.load_catalog()

    pool = client.free_pool()
    assert "google/lyria-3-pro-preview" not in pool, "text+audio output is a generator"
    assert pool[0] == "free/text-large", "the longest-context real chat model wins"
    await client.aclose()


async def test_classifiers_are_excluded(settings):
    client = _catalog_client(free_settings(settings))
    await client.load_catalog()
    assert not any("content-safety" in m for m in client.free_pool())
    await client.aclose()


async def test_audio_capable_free_models_are_tracked(settings):
    client = _catalog_client(free_settings(settings))
    await client.load_catalog()

    assert client.supports_free_audio() is True
    assert client.free_pool(audio=True) == ["free/omni-audio"]
    await client.aclose()


# -- rotation -------------------------------------------------------------
def _rotating_handler(exhausted: dict[str, int], seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        body = json.loads(request.content)
        model = body["model"]
        seen.append(model)
        if model in exhausted:
            return httpx.Response(exhausted[model], text="quota")
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    return handler


async def test_exhausted_model_is_skipped_for_the_next_one(settings):
    seen: list[str] = []
    client = LLMClient(
        free_settings(settings),
        transport=httpx.MockTransport(_rotating_handler({"free/text-large": 402}, seen)),
    )
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok, "a spent free model must not end the turn"
    assert seen[0] == "free/text-large"
    assert seen[1] != "free/text-large", "it moved on to the next model"
    await client.aclose()


async def test_rate_limit_pauses_the_account_instead_of_touring_the_models(settings):
    """A free-tier 429 covers the account, so the next model is just as limited."""
    seen: list[str] = []
    all_limited = dict.fromkeys(
        ["free/text-large", "free/omni-audio", "free/vision", "free/text-small"], 429
    )
    client = LLMClient(
        free_settings(settings),
        transport=httpx.MockTransport(_rotating_handler(all_limited, seen)),
    )
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind == "throttled"
    assert len(seen) == 1, "one request is enough to learn the account is limited"
    assert client.throttled_for() > 0
    await client.aclose()


async def test_further_requests_are_skipped_while_paused(settings):
    seen: list[str] = []
    client = LLMClient(
        free_settings(settings),
        transport=httpx.MockTransport(_rotating_handler({"free/text-large": 429}, seen)),
    )
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="anything")
    before = len(seen)
    result = await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert len(seen) == before, "no point knocking while the account is paused"
    assert result.error_kind == "throttled"
    await client.aclose()


async def test_requests_are_spaced_out_by_the_shared_allowance(settings, monkeypatch):
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr("asyncio.sleep", record)
    seen: list[str] = []
    client = LLMClient(
        free_settings(settings, free_rpm=60),  # one per second
        transport=httpx.MockTransport(_rotating_handler({}, seen)),
    )
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="anything")
    await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert len(seen) == 2
    assert slept, "the second request waited for its slot"
    assert max(slept) <= 1.0


async def test_pacing_is_off_for_paid_models(settings, monkeypatch):
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr("asyncio.sleep", record)
    seen: list[str] = []
    client = LLMClient(
        settings.replace(free_rpm=60),  # free_mode stays off
        transport=httpx.MockTransport(_rotating_handler({}, seen)),
    )
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="paid/big")
    await client.chat([{"role": "user", "content": "two"}], model="paid/big")

    assert slept == [], "paid accounts are not on the shared free allowance"
    await client.aclose()


async def test_a_spent_model_is_not_retried_on_the_next_message(settings):
    seen: list[str] = []
    client = LLMClient(
        free_settings(settings),
        transport=httpx.MockTransport(_rotating_handler({"free/text-large": 402}, seen)),
    )
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="anything")
    seen.clear()
    await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert "free/text-large" not in seen, "the exhausted model rests, it is not retried"
    await client.aclose()


async def test_every_model_exhausted_reports_it(settings, monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    seen: list[str] = []
    all_spent = {
        "free/text-large": 402, "free/vision": 402,
        "free/omni-audio": 402, "free/text-small": 402,
    }
    client = LLMClient(
        free_settings(settings),
        transport=httpx.MockTransport(_rotating_handler(all_spent, seen)),
    )
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind == "payment"
    assert len(set(seen)) > 1, "it tried more than one before giving up"
    await client.aclose()


async def test_startup_log_names_the_model_that_reads_media(settings):
    """The media rows must resolve with the flags a real media turn carries."""
    client = _catalog_client(free_settings(settings))
    await client.load_catalog()

    assert client.resolve("anything", vision=True) == "free/vision"
    assert client.resolve("anything", audio=True) == "free/omni-audio"
    assert client.resolve("anything") == "free/text-large"
    await client.aclose()


async def test_fallback_chain_respects_the_api_limit(settings):
    """OpenRouter rejects a `models` array longer than three entries."""
    bodies: list[dict] = []
    client = _catalog_client(free_settings(settings), chat_body=bodies)
    await client.load_catalog()

    assert len(client.free_pool()) > 3, "the pool must be longer than the cap to matter"
    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    chain = bodies[0]["models"]
    assert len(chain) <= 3
    assert chain[0] == bodies[0]["model"]
    assert len(set(chain)) == len(chain), "no duplicates in the chain"
    await client.aclose()


def _empty_from(broken: set[str], seen: list[str]):
    """A model that answers with nothing at all, as seen in production."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        body = json.loads(request.content)
        model = body["model"]
        seen.append(model)
        content = "" if model in broken else "ehehe~"
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "cost": 0},
            },
        )

    return handler


async def test_a_silent_model_is_swapped_not_retried(settings, monkeypatch):
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr("asyncio.sleep", record)
    seen: list[str] = []  # pacing is disabled here, so any sleep is a backoff
    client = LLMClient(
        free_settings(settings),
        transport=httpx.MockTransport(_empty_from({"free/text-large"}, seen)),
    )
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok, "another model should have answered"
    assert seen.count("free/text-large") == 1, "the silent model is tried once, not five times"
    assert slept == [], "swapping beats waiting for a model that returns nothing"
    await client.aclose()


async def test_reasoning_is_dropped_before_blaming_the_model(settings, monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        body = json.loads(request.content)
        bodies.append(body)
        content = "" if "reasoning" in body else "ehehe~"
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    client = LLMClient(free_settings(settings), transport=httpx.MockTransport(handler))
    await client.load_catalog()

    result = await client.chat(
        [{"role": "user", "content": "hi"}], model="anything", reasoning={"max_tokens": 0}
    )

    assert result.ok
    assert bodies[0]["model"] == bodies[1]["model"], "the same model gets a second chance"
    assert "reasoning" not in bodies[1]
    await client.aclose()


async def test_a_silent_model_rests_for_later_messages(settings, monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    seen: list[str] = []
    client = LLMClient(
        free_settings(settings),
        transport=httpx.MockTransport(_empty_from({"free/text-large"}, seen)),
    )
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="anything")
    seen.clear()
    await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert "free/text-large" not in seen, "a silent model is skipped on the next message"
    await client.aclose()


# -- quality guards for small models --------------------------------------
def test_compact_persona_is_small_enough_for_a_small_model():
    from astolfo import persona

    full = persona.static_prompt(locale="fa")
    compact = persona.compact_prompt(locale="fa")

    assert len(compact) < len(full) / 4, "the point is to fit a small context"
    lowered = compact.lower()
    for essential in ("astolfo", "newest message", "no markdown", "never invent"):
        assert essential in lowered, f"compact prompt dropped {essential!r}"
    assert "<identity>" not in compact, "no tag scaffolding for a model that copies it"


def test_compact_persona_keeps_one_example_in_the_right_language():
    from astolfo import persona

    assert "آستولفو" in persona.compact_prompt(locale="fa")
    assert "آستولفو" not in persona.compact_prompt(locale="en")
    assert "[teasing]" not in persona.compact_prompt(locale="fa"), "one example, not four"


async def test_free_mode_sends_the_compact_persona(rt, llm):
    rt.settings = rt.settings.replace(free_mode=True)
    await chat.handle_message(
        make_update(FakeMessage("astolfo hey")), FakeContext(rt, FakeBot())
    )
    assert "<identity>" not in llm.calls[0]["messages"][0]["content"]

    rt.settings = rt.settings.replace(free_mode=False)
    llm.calls.clear()
    await chat.handle_message(
        make_update(FakeMessage("astolfo hey", chat_id=-200)), FakeContext(rt, FakeBot())
    )
    assert "<identity>" in llm.calls[0]["messages"][0]["content"]


async def test_a_reply_that_leaks_the_prompt_is_retried_elsewhere(rt, llm):
    rt.settings = rt.settings.replace(free_mode=True)
    replies = iter(["<identity> You are Astolfo, the Rider", "ehehe~ hi!"])
    rested: list[str] = []

    async def flaky(messages, **kwargs):
        from astolfo.llm import ChatResult

        llm.calls.append({"messages": messages, **kwargs})
        return ChatResult(text=next(replies), model=f"free/m{len(llm.calls)}", usage=llm.usage)

    llm.chat = flaky
    llm.mark_unusable = lambda model, *a, **k: rested.append(model)

    message = FakeMessage("astolfo hey")
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    assert len(llm.calls) == 2, "the broken reply was not sent, another model was asked"
    assert message.sent == ["ehehe~ hi!"]
    assert rested == ["free/m1"], "the leaking model was retired"


async def test_two_broken_replies_do_not_reach_the_chat(rt, llm):
    rt.settings = rt.settings.replace(free_mode=True)

    async def always_broken(messages, **kwargs):
        from astolfo.llm import ChatResult

        llm.calls.append({"messages": messages, **kwargs})
        return ChatResult(text="assistant: hello", model="free/bad", usage=llm.usage)

    llm.chat = always_broken
    llm.mark_unusable = lambda *a, **k: None

    message = FakeMessage("astolfo hey")
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    assert len(llm.calls) == 2, "one retry, then give up"
    assert message.sent == [rt.strings("error_reply")], "garbage is never forwarded"


async def test_paid_mode_does_not_second_guess_replies(rt, llm):
    llm.reply = "assistant: hello"  # would be rejected in free mode
    message = FakeMessage("astolfo hey")
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    assert len(llm.calls) == 1
    assert message.sent, "paid models are trusted, no extra call"


async def test_misbehaving_models_sink_in_the_pool(settings):
    client = _catalog_client(free_settings(settings))
    await client.load_catalog()

    best = client.free_pool()[0]
    client.mark_unusable(best, seconds=0.0)  # rested, but instantly available again

    assert client.free_pool()[0] != best, "a model that misbehaved is no longer first"
    assert best in client.free_pool(), "but it stays available as a last resort"
