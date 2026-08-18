"""Stacking several services, each with its own free allowance."""

from __future__ import annotations

import json

import httpx

from astolfo import providers as providers_mod
from astolfo.llm import LLMClient


def _settings(settings, **overrides):
    return settings.replace(**{"free_mode": True, "free_rpm": 0, **overrides})


# -- configuration --------------------------------------------------------
def test_only_services_with_a_key_are_used(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("GROQ_API_KEY", "gq")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    found = providers_mod.discover(["openrouter", "google", "groq"])
    assert [p.name for p in found] == ["openrouter", "groq"]


def test_order_is_respected(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("GROQ_API_KEY", "gq")

    found = providers_mod.discover(["groq", "openrouter"])
    assert [p.name for p in found] == ["groq", "openrouter"]


def test_endpoint_and_models_are_overridable(monkeypatch):
    """A service changing its URL should be a config edit, not a release."""
    monkeypatch.setenv("GROQ_API_KEY", "gq")
    monkeypatch.setenv("GROQ_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GROQ_MODELS", "a/one, a/two")

    provider = providers_mod.discover(["groq"])[0]
    assert provider.chat_url == "https://example.test/v1/chat/completions"
    assert provider.models == ["a/one", "a/two"]


def test_unknown_names_are_reported_not_swallowed():
    assert providers_mod.unknown_names(["openrouter", "wat"]) == ["wat"]


def test_a_lone_key_still_works(settings, monkeypatch):
    """The single-service setup must keep working untouched."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = LLMClient(settings)
    assert [p.name for p in client.providers] == ["openrouter"]


# -- failover -------------------------------------------------------------
CATALOG = {
    "data": [
        {
            "id": "free/one",
            "context_length": 100000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        }
    ]
}


def _router(status_by_host: dict[str, int], seen: list[tuple[str, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        model = json.loads(request.content)["model"]
        seen.append((host, model))
        status = status_by_host.get(host, 200)
        if status != 200:
            return httpx.Response(status, text="no allowance left")
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": "ehehe~"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    return handler


def _multi(settings, monkeypatch, handler, order=("openrouter", "google")):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    cfg = _settings(settings, providers=list(order))
    return LLMClient(cfg, transport=httpx.MockTransport(handler))


async def test_a_spent_service_hands_over_to_the_next(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok, "the second service answered"
    hosts = [host for host, _ in seen]
    assert hosts[0] == "openrouter.ai"
    assert "generativelanguage.googleapis.com" in hosts


async def test_each_service_is_asked_for_its_own_models(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    by_host = dict(seen)
    assert by_host["openrouter.ai"] == "free/one", "discovered from its catalog"
    assert by_host["generativelanguage.googleapis.com"].startswith("gemini")


async def test_a_rested_service_is_skipped_next_time(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="anything")
    seen.clear()
    await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert all(host != "openrouter.ai" for host, _ in seen), "no point asking again"


async def test_exhausting_everything_is_reported_as_throttled(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    everything = {"openrouter.ai": 429, "generativelanguage.googleapis.com": 429}
    client = _multi(settings, monkeypatch, _router(everything, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind == "throttled"
    assert client.throttled_for() > 0
    assert len({host for host, _ in seen}) == 2, "both were tried before giving up"


async def test_a_bad_request_does_not_burn_the_other_services(settings, monkeypatch):
    """Only an exhausted allowance means try elsewhere; our own mistake does not."""
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 400}, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind is None
    assert len({host for host, _ in seen}) == 1, "the second service was never asked"
