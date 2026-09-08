"""A refusal that names the model is about the model, not about the key.

From the box, with the bot answering nothing at all for hours:

    15:49:36 | openrouter/thinkingmachines/inkling-small:free: HTTP 403 the
               request never reached the service, resting 10m - "thinking
               machines/inkling-small:free is only available on agentic
               harnesses. Try plugging it into a coding agent..."
    15:50:21 | no completion for chat ...: openrouter has no usable key right now

OpenRouter gates some free endpoints. The key reached the gate to be told so -
which means it authenticated - and the 403 was charged to the key anyway. That
closes a loop: the model stays first in the pool because nothing was ever written
against it, so the next turn picks it again, is refused again, and rests another
key. Three keys later the service has none left and the panel reports a refused
key to an owner whose key is fine.
"""

from __future__ import annotations

import time

import httpx

from astolfo.llm import AUTH_COOLDOWN, FORBIDDEN_COOLDOWN, LLMClient

GATED = "thinkingmachines/inkling-small:free"
GATE = (
    f"{GATED} is only available on agentic harnesses. Try plugging it into a "
    "coding agent or productivity app listed on https://openrouter.ai/apps"
)


CATALOG = {
    "data": [
        {
            # The head of the pool, and the reason it is: a million-token window
            # sorts first, and nothing about the listing says it will not serve.
            "id": GATED,
            "context_length": 1000000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
            },
        },
        {
            "id": "meta-llama/llama-3.3-70b-instruct:free",
            "context_length": 128000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        },
    ]
}


async def _ready(settings, monkeypatch, answer, services=("openrouter",)) -> LLMClient:
    """A client whose free pool was discovered the way the box discovers it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        return answer(request)

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    client = LLMClient(
        # free_rpm=0: the 7.5s spacing between free calls is real and tested in
        # test_pacing; here it would only make these tests sleep.
        settings.replace(providers=list(services), free_mode=True, free_rpm=0),
        transport=httpx.MockTransport(handler),
    )
    await client.load_catalog()
    assert client.free_pool()[0] == GATED, "the gated model should be picked first"
    return client


def _gate_then_answer(seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if f'"model": "{GATED}"' in body or f'"model":"{GATED}"' in body:
            seen.append(GATED)
            return httpx.Response(403, text=GATE)
        seen.append("other")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}]}
        )

    return handler


async def test_the_turn_goes_on_with_the_next_model(settings, monkeypatch):
    seen: list[str] = []
    client = await _ready(settings, monkeypatch, _gate_then_answer(seen))

    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert result.ok, f"the gate ended the turn: {result.error}"
    assert seen[0] == GATED and "other" in seen, f"it never moved on: {seen}"
    await client.aclose()


async def test_the_key_is_not_rested_for_a_model_it_cannot_serve(settings, monkeypatch):
    """The key authenticated - it reached the gate to be refused by it."""
    client = await _ready(settings, monkeypatch, _gate_then_answer([]))
    credential = client.providers[0].credentials[0]

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert credential.rested_until == 0.0, (
        "a working key was rested over a model it is not allowed to call"
    )
    await client.aclose()


async def test_the_service_is_not_rested_either(settings, monkeypatch):
    """Two services, because resting the provider is guarded by having a second
    one to fall back on - with a single service that branch is unreachable and
    the test would pass without proving anything."""
    client = await _ready(
        settings, monkeypatch, _gate_then_answer([]), services=("openrouter", "google")
    )
    provider = client.providers[0]

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert provider.paused_until <= time.monotonic(), (
        "the whole service was benched over one gated model"
    )
    await client.aclose()


async def test_the_model_is_the_thing_that_rests(settings, monkeypatch):
    """Without this the model stays first in the pool and the loop closes."""
    client = await _ready(settings, monkeypatch, _gate_then_answer([]))

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert client._cooldowns.get(GATED, 0.0) > time.monotonic(), (
        "nothing was written against the model, so the next turn picks it again"
    )
    assert client._strikes.get(GATED, 0) >= 1, "the strike was not recorded"
    await client.aclose()


async def test_a_403_that_does_not_name_a_model_still_rests_the_key(settings, monkeypatch):
    """The edge block v2.8.4 is about. The two must stay distinguishable: one is
    a claim about the request, the other about this model in particular."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="the request needs to be authenticated")

    client = await _ready(settings, monkeypatch, handler)
    credential = client.providers[0].credentials[0]

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    rest = credential.rested_until - time.time()
    assert 0 < rest <= FORBIDDEN_COOLDOWN + 5, f"the key was not rested: {rest}"
    assert client._cooldowns.get(GATED, 0.0) <= time.monotonic(), (
        "an edge block was blamed on the model"
    )
    await client.aclose()


async def test_the_advice_line_is_only_for_a_refused_key(settings, monkeypatch, caplog):
    """"check OPENROUTER_API_KEY" after a 403 names the one thing that is fine."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="the request needs to be authenticated")

    client = await _ready(settings, monkeypatch, handler)

    with caplog.at_level("ERROR"):
        await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert "check OPENROUTER_API_KEY" not in caplog.text, (
        "a 403 sent the owner to check a key that authenticates"
    )
    await client.aclose()


async def test_a_401_still_says_to_check_the_key(settings, monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    client = await _ready(settings, monkeypatch, handler)
    credential = client.providers[0].credentials[0]

    with caplog.at_level("ERROR"):
        await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert "check OPENROUTER_API_KEY" in caplog.text
    assert credential.rested_until - time.time() > FORBIDDEN_COOLDOWN * 2
    assert credential.rested_until - time.time() <= AUTH_COOLDOWN + 5
    await client.aclose()


# -- and what the panel says when every key is mid-rest --------------------
async def test_every_key_resting_is_not_a_refused_key(settings, monkeypatch):
    """The panel's own words, from the diagnostics: "❌ openrouter: the key was
    refused" - for keys that were serving out a rest earned earlier. It is the
    v2.8.4 defect again, reached through `pick()` returning nothing rather than
    through a live 401, and it sends the owner to replace a key that works."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    client = await _ready(settings, monkeypatch, handler)
    for credential in client.providers[0].credentials:
        credential.rested_until = time.time() + 600

    ok, said = await client.probe("openrouter")

    assert not ok
    assert "the key was refused" not in said, (
        f"a resting key was called a refused one: {said!r}"
    )
    assert "resting" in said
    await client.aclose()


async def test_the_resting_answer_names_the_soonest_key(settings, monkeypatch):
    """The same `min`-not-`max` v2.8.5 fixed for services, now for keys: one key
    resting a day beside one resting a minute is a minute away."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    client = await _ready(settings, monkeypatch, handler)
    provider = client.providers[0]
    provider.credentials.append(
        type(provider.credentials[0])(value="second", id=7)
    )
    provider.credentials[0].rested_until = time.time() + AUTH_COOLDOWN
    provider.credentials[1].rested_until = time.time() + 120

    ok, said = await client.probe("openrouter")

    assert not ok
    assert "1440m" not in said, f"it quoted the longest wait: {said!r}"
    assert "2m" in said, said
    await client.aclose()
