"""A model id belongs to the service that listed it, in the payload too.

OpenRouter takes a `models` array of alternatives to try when the first is rate
limited. It was filled from the global free pool, and since every service's
catalog is read that pool holds Google, Cohere and Mistral ids - so OpenRouter
was handed one of Google's and rejected the whole request:

    openrouter does not serve minimax/minimax-m3:free
    error.message = models/gemini-2.5-flash is not a valid model ID

The reply names the foreign id; the code read "is not a valid model ID", blamed
the model in the `model` field, and retired it. Every model in the pool was
condemned in turn for a defect in a field none of them appeared in: 99 disowned
warnings and three answered turns in three hours.

`_candidates` was given the service filter when the catalog went multi-service.
This call site was not, and nothing tested it.
"""

from __future__ import annotations

import httpx

from astolfo.catalog import Model
from astolfo.llm import MAX_FALLBACKS, LLMClient
from tests.test_free_mode import free_settings

# One free model from each of three services, which is what the live box has.
OPENROUTER = ["minimax/minimax-m3:free", "thinkingmachines/inkling:free"]
GOOGLE = ["models/gemini-2.5-flash", "models/gemini-flash-lite-latest"]
COHERE = ["command-r-08-2024"]

FOREIGN = set(GOOGLE) | set(COHERE)


def _client(settings) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    return LLMClient(free_settings(settings), transport=httpx.MockTransport(handler))


def _mixed_catalog(client: LLMClient) -> None:
    """A pool exactly as the live catalog builds it: every service in one list."""
    client._models = [
        *(Model(id=name, service="openrouter") for name in OPENROUTER),
        *(Model(id=name, service="google") for name in GOOGLE),
        *(Model(id=name, service="cohere") for name in COHERE),
    ]
    client._free_text = [*OPENROUTER, *GOOGLE, *COHERE]


def _openrouter(client: LLMClient):
    return next(p for p in client.providers if p.name == "openrouter")


def _payload_for(client: LLMClient, model: str) -> dict:
    return client._payload(
        provider=_openrouter(client),
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.9,
        max_tokens=100,
        reasoning=None,
        web=False,
        response_format=None,
        fallbacks=True,
    )


async def test_no_other_services_ids_reach_openrouter(settings):
    """The outage, in one assertion."""
    client = _client(settings)
    _mixed_catalog(client)

    payload = _payload_for(client, "minimax/minimax-m3:free")

    assert set(payload["models"]) & FOREIGN == set(), (
        f"a foreign id went to OpenRouter: {payload['models']}"
    )
    await client.aclose()


async def test_the_alternatives_are_that_services_own_models(settings):
    client = _client(settings)
    _mixed_catalog(client)

    payload = _payload_for(client, "minimax/minimax-m3:free")

    assert payload["models"][0] == "minimax/minimax-m3:free", "the primary comes first"
    assert set(payload["models"]) <= set(OPENROUTER)
    assert len(payload["models"]) <= MAX_FALLBACKS
    await client.aclose()


async def test_a_service_with_nothing_of_its_own_gets_no_chain(settings):
    """Rather than being handed somebody else's list."""
    client = _client(settings)
    client._models = [Model(id=name, service="google") for name in GOOGLE]
    client._free_text = list(GOOGLE)

    payload = _payload_for(client, "models/gemini-2.5-flash")

    assert set(payload.get("models", [])) & set(GOOGLE) == set()
    await client.aclose()


# -- and when a refusal names an id we did not ask for --------------------
async def test_a_refusal_naming_another_id_leaves_the_model_alone(settings):
    """The second half of the outage: the wrong model was being condemned."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        import json as _json

        asked.append(_json.loads(request.content)["model"])
        return httpx.Response(
            400,
            json={"error": {"message": "models/gemini-2.5-flash is not a valid model ID"}},
        )

    client = LLMClient(free_settings(settings), transport=httpx.MockTransport(handler))
    _mixed_catalog(client)

    result = await client.chat([{"role": "user", "content": "hi"}], model=OPENROUTER[0])

    assert not result.ok
    assert client._unknown == set(), "an innocent model was retired"
    assert len(asked) == 1, "and the pool was not walked looking for a culprit"
    await client.aclose()


async def test_a_refusal_naming_the_model_asked_for_still_retires_it(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        import json as _json

        model = _json.loads(request.content)["model"]
        return httpx.Response(
            400, json={"error": {"message": f"{model} is not a valid model ID"}}
        )

    client = LLMClient(free_settings(settings), transport=httpx.MockTransport(handler))
    _mixed_catalog(client)

    await client.chat([{"role": "user", "content": "hi"}], model=OPENROUTER[0])

    assert ("openrouter", OPENROUTER[0]) in client._unknown
    await client.aclose()


# -- and the log naming the answer that was actually sent -----------------
async def test_the_route_log_names_the_model_whose_reply_was_sent(rt, llm, caplog):
    """The first answer is logged before the quality check. When it is rejected
    and a second model produces the one that ships, the log named the first."""
    import logging

    from astolfo.llm import ChatResult
    from tests.conftest import FakeMessage
    from tests.test_chat import run

    rt.settings = rt.settings.replace(free_mode=True)
    replies = [
        # Rejected: a reply that is nothing but its own repetition.
        ChatResult(text=" ".join(["خیلی قوی آره"] * 9), model="bad/model", service="openrouter"),
        ChatResult(text="نمی‌دونم والا، تو بگو چی فکر می‌کنی", model="good/model",
                   service="google"),
    ]

    async def answer(messages, **kwargs):
        return replies.pop(0) if replies else replies[-1]

    llm.chat = answer
    llm.stuck_on = lambda model: False

    with caplog.at_level(logging.INFO, logger="astolfo.chat"):
        await run(rt, FakeMessage("astolfo نظرت چیه"))

    routes = [r.message for r in caplog.records if " | " in str(r.message)]
    assert routes, "the turn should have logged a route"
    assert any("good/model" in line for line in routes), (
        f"the log never named the model that answered: {routes}"
    )
