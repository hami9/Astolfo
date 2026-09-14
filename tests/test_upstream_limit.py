"""A 429 that names the model is a limit on that model, not on the account.

From the box, every few minutes for three days, on a key that worked:

    openrouter/google/gemma-4-26b-a4b-it:free: HTTP 429 too many requests per
        minute, resting 1m - "Provider returned error 429 google/gemma-4-26b-a4b-it:free
        is temporarily rate-limited upstream. Please retry shortly, or add your own
        key to accumulate your rate limits..."
    openrouter is out of allowance, resting it for 60s; nothing left to try

The free capacity behind that one model was gone. In free mode every 429 was
taken to be the account's, so the service sat out a minute, nothing was written
against the model, and the next turn picked the same model from the head of the
pool to be refused the same way: 99 turns without an answer in three days.
"""

from __future__ import annotations

import json
import time

import httpx

from astolfo.llm import RATE_LIMIT_COOLDOWN, UPSTREAM_SWITCHES, LLMClient

LIMITED = "google/gemma-4-26b-a4b-it:free"
OTHERS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "poolside/laguna-s-2.1:free",
    "liquid/lfm-2.5-2.6b:free",
]


def _listing(model_id: str, context: int) -> dict:
    return {
        "id": model_id,
        "context_length": context,
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }


# The limited model has the widest window, which is what puts it first in the pool.
CATALOG = {
    "data": [_listing(LIMITED, 1_000_000)]
    + [_listing(model_id, 128_000 - n) for n, model_id in enumerate(OTHERS)]
}


def _upstream(model_id: str) -> httpx.Response:
    """OpenRouter's own shape: the provider's sentence sits in `metadata.raw`."""
    return httpx.Response(
        429,
        json={
            "error": {
                "message": "Provider returned error",
                "code": 429,
                "metadata": {
                    "raw": f"{model_id} is temporarily rate-limited upstream. Please retry "
                    "shortly, or add your own key to accumulate your rate limits: "
                    "https://openrouter.ai/settings/integrations",
                    "provider_name": "Google AI Studio",
                },
            }
        },
    )


def _answer() -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


def _only_the_first_is_limited(model: str) -> httpx.Response:
    return _upstream(model) if model == LIMITED else _answer()


async def _ready(settings, monkeypatch, answer, registry=None):
    """A client whose free pool was discovered the way the box discovers it, and
    the models its chat requests asked for, in order."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        model = json.loads(request.read())["model"]
        asked.append(model)
        return answer(model)

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(
        # free_rpm=0: the spacing between free calls is real and tested in
        # test_pacing; here it would only make these tests sleep.
        settings.replace(providers=["openrouter"], free_mode=True, free_rpm=0),
        transport=httpx.MockTransport(handler),
        registry=registry,
    )
    await client.load_catalog()
    assert client.free_pool()[0] == LIMITED, "the limited model should be picked first"
    return client, asked


def _registry(settings):
    from astolfo.crypto import SecretBox
    from astolfo.db import open_database
    from astolfo.services import ServiceRegistry

    return ServiceRegistry(open_database(settings.data_dir), SecretBox(settings.data_dir))


async def test_the_turn_goes_on_with_the_next_model(settings, monkeypatch):
    client, asked = await _ready(settings, monkeypatch, _only_the_first_is_limited)

    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert result.ok, f"one limited model ended the turn: {result.error}"
    assert asked[0] == LIMITED and len(asked) == 2, asked
    await client.aclose()


async def test_the_service_is_not_paused(settings, monkeypatch):
    client, _ = await _ready(settings, monkeypatch, _only_the_first_is_limited)

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert client.providers[0].paused_until <= time.monotonic(), (
        "the whole service sat out a limit that belonged to one model"
    )
    await client.aclose()


async def test_the_key_is_not_rested(settings, monkeypatch):
    client, _ = await _ready(settings, monkeypatch, _only_the_first_is_limited)
    credential = client.providers[0].credentials[0]

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert credential.rested_until == 0.0
    await client.aclose()


async def test_the_next_turn_starts_with_another_model(settings, monkeypatch):
    """The loop itself: nothing was written against the model, so every turn
    began with it."""
    client, asked = await _ready(settings, monkeypatch, _only_the_first_is_limited)

    await client.chat([{"role": "user", "content": "hi"}], model="m")
    first_turn = len(asked)
    await client.chat([{"role": "user", "content": "hi again"}], model="m")

    assert asked[first_turn] != LIMITED, f"the next turn began with the limited model: {asked}"
    rest = client._cooldowns.get(LIMITED, 0.0) - time.monotonic()
    assert RATE_LIMIT_COOLDOWN - 5 < rest <= RATE_LIMIT_COOLDOWN, rest
    await client.aclose()


async def test_a_limit_is_not_a_strike(settings, monkeypatch):
    """Strikes are for a model that answers with nothing or nonsense, and they
    sink it across restarts. A model that is only busy comes back level."""
    client, _ = await _ready(settings, monkeypatch, _only_the_first_is_limited)

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert client._strikes.get(LIMITED, 0) == 0
    await client.aclose()


async def test_a_429_that_names_no_model_still_pauses_the_service(settings, monkeypatch):
    """A limit on the account: every model behind it is equally limited, so
    walking them would only spend calls on being told so."""

    def account_limit(model: str) -> httpx.Response:
        message = "Rate limit exceeded: free-models-per-min."
        return httpx.Response(429, json={"error": {"message": message, "code": 429}})

    client, asked = await _ready(settings, monkeypatch, account_limit)

    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert not result.ok and result.error_kind == "throttled", result
    assert asked == [LIMITED], f"a limit on the account walked the pool: {asked}"
    assert client.providers[0].paused_until > time.monotonic()
    await client.aclose()


async def test_a_limit_on_every_model_is_the_accounts_after_a_few(settings, monkeypatch):
    """Should a service ever quote back each id it is asked for, the turn stops
    after a few rather than walking the whole pool."""
    client, asked = await _ready(settings, monkeypatch, _upstream)

    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert not result.ok
    assert len(asked) == UPSTREAM_SWITCHES + 1, asked
    assert len(asked) < len(CATALOG["data"]), "the pool ran out before the cap was reached"
    assert client.providers[0].paused_until > time.monotonic()
    await client.aclose()


async def test_an_empty_wallet_is_not_read_as_one_model(settings, monkeypatch):
    """Credit belongs to the account even when the sentence names the model."""

    def spent(model: str) -> httpx.Response:
        message = f"No credits remaining for {model}. Add more credits to continue."
        return httpx.Response(429, json={"error": {"message": message}})

    client, asked = await _ready(settings, monkeypatch, spent)

    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert result.error_kind == "payment", result
    assert asked == [LIMITED], f"an empty wallet was walked model by model: {asked}"
    await client.aclose()


async def test_the_refusal_is_counted_beside_the_answer(settings, monkeypatch):
    """The day's usage only ever recorded answers, so the panel and the ranking
    saw a service that never failed."""
    registry = _registry(settings)
    client, _ = await _ready(settings, monkeypatch, _only_the_first_is_limited, registry)

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    today = registry.usage_today()["openrouter"]
    assert (today["requests"], today["failures"]) == (1, 1)
    await client.aclose()
