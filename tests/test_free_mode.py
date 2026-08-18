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
            "id": "paid/big",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "architecture": {"input_modalities": ["text", "image"]},
        },
        {
            "id": "free/text-small",
            "context_length": 8000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"]},
        },
        {
            "id": "free/text-large",
            "context_length": 128000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"]},
        },
        {
            "id": "free/vision",
            "context_length": 64000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text", "image"]},
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


def free_settings(settings) -> Settings:
    return settings.replace(free_mode=True)


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

    assert client.free_pool() == ["free/text-large", "free/vision", "free/text-small"]
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

    def resolve(self, model, *, vision=False):
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
