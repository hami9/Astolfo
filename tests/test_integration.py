"""End-to-end pipeline against a mocked HTTP transport.

These exercise the real LLMClient, so the outgoing request body is asserted exactly as
OpenRouter would receive it.
"""

from __future__ import annotations

import json
from dataclasses import fields

import httpx

from astolfo import chat
from astolfo.config import Settings
from astolfo.llm import LLMClient
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update


def _install_transport(rt, handler) -> None:
    client = LLMClient(rt.settings, transport=httpx.MockTransport(handler))
    rt.llm = client
    rt.router._llm = client


def _reply(text="hey there~", cost=0.0002):
    return {
        "model": "test/model",
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 2500, "completion_tokens": 30, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 2000}},
    }


async def test_full_turn_request_shape(rt):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_reply())

    _install_transport(rt, handler)
    message = FakeMessage("astolfo hey what's up")
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    body = captured["body"]
    assert message.sent == ["hey there~"]
    assert captured["auth"] == "Bearer test-key"
    assert body["model"] == rt.settings.model_fast
    assert body["max_tokens"] == rt.settings.max_tokens_fast
    assert body["usage"] == {"include": True}
    assert body["reasoning"] == {"max_tokens": 0}, "fast turns must not pay for thinking"
    assert "plugins" not in body

    roles = [m["role"] for m in body["messages"]]
    assert roles[:2] == ["system", "system"]
    assert roles[-1] == "user"
    assert "<identity>" in body["messages"][0]["content"]
    assert "<response-mode" in body["messages"][1]["content"]


async def test_search_turn_enables_the_web_plugin(rt):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_reply("about 1.2 million"))

    _install_transport(rt, handler)
    message = FakeMessage("astolfo what is the dollar price today?")
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    body = bodies[0]
    assert body["plugins"][0]["id"] == "web"
    assert body["temperature"] <= 0.3
    assert body["model"] == rt.settings.model_search


async def test_usage_and_cache_stats_are_recorded(rt):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply(cost=0.0025))

    _install_transport(rt, handler)
    await chat.handle_message(
        make_update(FakeMessage("astolfo hey")), FakeContext(rt, FakeBot())
    )

    summary = rt.budget.summary()
    assert summary["calls"] == 1
    assert summary["cost_today"] == 0.0025
    assert summary["cached_tokens"] == 2000
    assert summary["cache_hit_rate"] == 0.8


async def test_provider_outage_falls_back_to_in_character_line(rt, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    rt.settings = rt.settings.replace(max_retries=2)
    _install_transport(rt, handler)

    message = FakeMessage("astolfo hey")
    await chat.handle_message(make_update(message), FakeContext(rt, FakeBot()))
    assert message.sent == [rt.strings("error_reply")]


def test_every_setting_round_trips_through_the_environment(monkeypatch):
    """Guards against annotation drift breaking the env coercion table."""
    samples = {
        "str": "value",
        "int": "7",
        "float": "1.5",
        "bool": "false",
        "list[str]": "a,b",
        "list[int]": "1,2",
        "str | None": "price",
    }
    expected = {
        "str": "value",
        "int": 7,
        "float": 1.5,
        "bool": False,
        "list[str]": ["a", "b"],
        "list[int]": [1, 2],
        "str | None": "price",
    }

    for f in fields(Settings):
        annotation = str(f.type).replace('"', "").strip()
        if annotation not in samples:
            raise AssertionError(f"{f.name} has unsupported annotation {annotation!r}")
        if f.name in {"telegram_token", "api_key"}:
            continue

        monkeypatch.setenv(f.metadata["env"], samples[annotation])
        value = getattr(Settings.from_env(), f.name)
        assert value == expected[annotation], f"{f.name} coerced to {value!r}"
        monkeypatch.delenv(f.metadata["env"])
