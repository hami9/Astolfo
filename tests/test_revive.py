"""A service that answers must stop being treated as broken.

From the panel, on the live bot, at the same moment:

    test:     ✅ openrouter: answered by minimax/minimax-m3:free
    services: 💤 openrouter — resting for 374 more minutes — HTTP 403

It had answered, and the bot was still six hours into ignoring it. Meanwhile
cohere carried 172 calls on its own, which is why the replies stopped sounding
like anything.
"""

from __future__ import annotations

import time

import httpx

from astolfo.crypto import SecretBox
from astolfo.db import open_database
from astolfo.llm import AUTH_COOLDOWN, FORBIDDEN_COOLDOWN, LLMClient
from astolfo.services import ServiceRegistry


def _registry(settings):
    return ServiceRegistry(open_database(settings.data_dir), SecretBox(settings.data_dir))


def _client(settings, monkeypatch, handler, registry=None):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    cfg = settings.replace(providers=["openrouter"], free_mode=False)
    return LLMClient(cfg, transport=httpx.MockTransport(handler), registry=registry)


def _answers(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": []})
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


def _refuses(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(status, text="nope")

    return handler


# -- a passing test puts it back in the rotation --------------------------
async def test_a_service_that_answers_the_test_stops_resting(settings, monkeypatch):
    client = _client(settings, monkeypatch, _answers, _registry(settings))
    provider = client.providers[0]
    provider.paused_until = time.monotonic() + 6 * 3600

    ok, said = await client.probe("openrouter")

    assert ok and "answered" in said
    assert provider.paused_until == 0.0, "the rest it just disproved is cleared"
    assert client.usable_now(), "and the bot will actually use it again"


async def test_a_service_that_fails_the_test_goes_back_to_resting(settings, monkeypatch):
    """The rest is only cancelled by evidence, not by pressing the button."""
    client = _client(settings, monkeypatch, _refuses(401), _registry(settings))
    provider = client.providers[0]
    until = time.monotonic() + 6 * 3600
    provider.paused_until = until

    ok, _ = await client.probe("openrouter")

    assert not ok
    assert provider.paused_until >= until, "still resting, as it was"


async def test_ordinary_traffic_revives_it_too(settings, monkeypatch):
    """Not only the panel: any answer at all is proof it is well again."""
    client = _client(settings, monkeypatch, _answers, _registry(settings))
    provider = client.providers[0]
    provider.paused_until = time.monotonic() + 60

    # A paused provider is skipped, so this is the case where it is the last one
    # standing and gets tried anyway.
    await client._chat_with(
        provider, [{"role": "user", "content": "hi"}], model="m", temperature=0.0,
        max_tokens=5, reasoning=None, web=False, response_format=None,
        fallbacks=False, max_retries=1, vision=False, audio=False,
    )

    assert provider.paused_until == 0.0


async def test_reviving_survives_a_restart(settings, monkeypatch):
    """The rest is stored as wall clock, so clearing it has to be stored too."""
    registry = _registry(settings)
    registry.rest_service("openrouter", 6 * 3600, "HTTP 403")
    assert any(
        row["name"] == "openrouter" and row["rested_until"] > 0
        for row in registry.rows()
    )

    client = _client(settings, monkeypatch, _answers, registry)
    client.providers[0].paused_until = time.monotonic() + 6 * 3600
    await client.probe("openrouter")

    assert all(
        row.get("rested_until", 0) == 0
        for row in registry.rows()
        if row["name"] == "openrouter"
    ), "the stored rest is cleared, not just the one in memory"


# -- 401 is a claim about the key; 403 is not -----------------------------
async def test_a_401_still_costs_a_day(settings, monkeypatch):
    """Cerebras answers 401 and genuinely is not usable. That one is terminal."""
    client = _client(settings, monkeypatch, _refuses(401), _registry(settings))
    credential = client.providers[0].credentials[0]

    await client.probe("openrouter")

    assert credential.rested_until - time.time() > AUTH_COOLDOWN / 2
    assert "401" in credential.last_error and "key was refused" in credential.last_error


async def test_a_403_costs_ten_minutes_not_a_day(settings, monkeypatch):
    """OpenRouter answered a test while serving out a day earned by one 403."""
    client = _client(settings, monkeypatch, _refuses(403), _registry(settings))
    credential = client.providers[0].credentials[0]

    await client.probe("openrouter")

    waiting = credential.rested_until - time.time()
    assert waiting <= FORBIDDEN_COOLDOWN + 5
    assert waiting > 0, "it is still rested, just not for the rest of the day"
    assert "403" in credential.last_error


def test_the_two_cooldowns_are_far_apart() -> None:
    """If they were close the distinction would not be worth the code."""
    assert FORBIDDEN_COOLDOWN * 10 < AUTH_COOLDOWN
