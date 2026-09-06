"""A model the service has disowned must stay disowned.

The bot logs "it will not be asked again" and then asks again on the next
message: the memory is consulted when the first model is picked and on no other
path, so every turn walks the same dead pool. These are the tests for the paths
that were not covered.
"""

from __future__ import annotations

import json

import httpx

from astolfo.llm import ACCOUNT_DISOWNS, LLMClient
from tests.test_free_mode import CATALOG, free_settings

DISOWNED = {"error": {"message": "free/text-large is not a valid model ID"}}


def _reply(model: str) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": "ehehe~"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0},
    }


def _client(settings, *, disown=("free/text-large",), asked=None):
    """A catalog client that disowns the named models with a 400."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        model = json.loads(request.content)["model"]
        if asked is not None:
            asked.append(model)
        if model in disown:
            body = dict(DISOWNED)
            body["error"] = {"message": f"{model} is not a valid model ID"}
            return httpx.Response(400, json=body)
        return httpx.Response(200, json=_reply(model))

    return LLMClient(free_settings(settings), transport=httpx.MockTransport(handler))


async def _load(client) -> list[str]:
    await client.load_catalog()
    return client.free_pool()


async def test_a_disowned_model_is_not_asked_again_on_the_next_message(settings):
    """The first pick is already guarded, by `_candidates`. This holds that line."""
    asked: list[str] = []
    client = _client(settings, asked=asked)
    await _load(client)

    first = await client.chat([{"role": "user", "content": "hi"}], model="free/text-large")
    assert first.ok, "it should have failed over to a model the service does serve"
    assert "free/text-large" in asked

    asked.clear()
    second = await client.chat([{"role": "user", "content": "hi"}], model="free/text-large")

    assert second.ok
    assert "free/text-large" not in asked, "it was told once that this model is not served"
    await client.aclose()


async def test_an_empty_reply_does_not_fail_over_to_a_disowned_model(settings):
    """The path the log was actually walking.

    A model that answers with nothing hands the turn to `_next_free`, which reads
    the pool directly rather than through `_candidates` - so the one model the
    service has already refused is exactly what it offers next:

        cohere/north-mini-code:free returned nothing, switching to minimax-m3:free
        openrouter does not serve minimax/minimax-m3:free; it will not be asked again
    """
    client = _client(settings)
    pool = await _load(client)

    await client.chat([{"role": "user", "content": "hi"}], model="free/text-large")
    assert ("openrouter", "free/text-large") in client._unknown

    # Walk the failover to exhaustion, the way a run of empty replies does. One
    # step is not enough: a model that answered outranks the refused one by score,
    # so the refusal only surfaces further down the list.
    tried: set[str] = set()
    offered = []
    while (nxt := client._next_free(tried=tried, vision=False, audio=False)) is not None:
        offered.append(nxt)
        tried.add(nxt)

    assert offered, "the walk should have offered something"
    assert "free/text-large" not in offered, "the failover walk offered a refused model"
    assert pool[0] == "free/text-large", "the refused model was the pool's first choice"
    await client.aclose()


async def test_a_disowned_model_leaves_the_free_pool(settings):
    """`free_pool` is what `resolve` and every failover step read."""
    client = _client(settings)
    pool = await _load(client)
    assert "free/text-large" in pool

    await client.chat([{"role": "user", "content": "hi"}], model="free/text-large")

    assert "free/text-large" not in client.free_pool()
    assert client.resolve("free/text-large") != "free/text-large"
    await client.aclose()


async def test_a_disowned_model_is_not_resurrected_when_the_rest_are_resting(settings):
    """`_usable` tries everything again rather than going silent. That is right for
    a resting model - not now - and wrong for a disowned one - never."""
    client = _client(settings)
    pool = await _load(client)
    client._unknown.add(("openrouter", pool[0]))
    for model in pool[1:]:
        client.mark_unusable(model)

    assert pool[0] not in client.free_pool()
    await client.aclose()


async def test_when_every_model_is_disowned_it_says_so_rather_than_asking(settings):
    """Reached by hand, because the threshold now stops the walk long before here."""
    asked: list[str] = []
    client = _client(settings, disown=(), asked=asked)
    pool = await _load(client)
    for model in pool:
        client._unknown.add(("openrouter", model))

    assert client.free_pool() == []
    asked.clear()

    result = await client.chat([{"role": "user", "content": "hi"}], model=pool[0])

    assert not result.ok
    assert asked == [], "nothing is left to ask, so nothing should have been asked"
    await client.aclose()


async def test_one_account_refusing_everything_rests_the_service(settings):
    """Sixteen models from six vendors do not stop existing in two minutes.

    Past the threshold the fact is about the account, so the service rests and
    the models are given back rather than being written off one at a time.
    """
    client = _client(settings)
    pool = await _load(client)
    assert len(pool) > ACCOUNT_DISOWNS, "the catalog must be able to cross the threshold"

    await client.aclose()
    client = _client(settings, disown=tuple(pool))
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "hi"}], model=pool[0])

    provider = client.providers[0]
    assert provider.paused_until > 0, "the service should be resting, not the models"
    assert client._unknown == set(), "the models were given back"
    await client.aclose()
