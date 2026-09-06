"""A 403 is not a 401, and the service must not be benched for a day over one.

From a live diagnostics, with the service that had just started working again:

    openrouter   yes  1/1   1429m    24m ago   ... HTTP 403 the request n...

Twenty-three hours and forty-nine minutes of rest, earned by one 403, on a
service that answered twenty-four minutes earlier.

`FORBIDDEN_COOLDOWN` exists for exactly this and says so in its own comment. It
was applied to the credential and not to the provider beside it - the same shape
as the fallback ids that were scoped in one call site and not its sibling.
"""

from __future__ import annotations

import httpx

from astolfo.llm import AUTH_COOLDOWN, FORBIDDEN_COOLDOWN, LLMClient
from tests.test_free_mode import free_settings


def _client(settings, status: int, body: str) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(status, text=body)

    # Two services, so the code reaches the branch that rests this one and moves on.
    return LLMClient(
        free_settings(settings, providers=["openrouter", "google"]),
        transport=httpx.MockTransport(handler),
    )


def _rest(client: LLMClient) -> float:
    import time

    provider = client.providers[0]
    return provider.paused_until - time.monotonic()


async def test_a_403_rests_the_service_for_minutes_not_a_day(settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    client = _client(settings, 403, "the request needs to be authenticated")

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    rest = _rest(client)
    assert rest <= FORBIDDEN_COOLDOWN + 5, f"benched for {rest / 3600:.1f}h over a 403"
    await client.aclose()


async def test_a_401_still_rests_it_for_the_day(settings, monkeypatch):
    """The distinction has to stay a distinction: a refused key is not transient."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    client = _client(settings, 401, "invalid api key")

    await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert _rest(client) > FORBIDDEN_COOLDOWN * 2
    assert _rest(client) <= AUTH_COOLDOWN + 5
    await client.aclose()


async def test_a_403_is_not_reported_as_a_refused_key(settings, monkeypatch):
    """The panel said "the key was refused" for a key that was working.

    `faults` reads this 403 as blocked - "the request never reached the service"
    - and the panel's own refusal line said exactly that, while its key test said
    the opposite. A request stopped at the edge says nothing about the key, and
    the difference decides whether somebody goes and replaces a good one.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    client = _client(settings, 403, "the request needs to be authenticated")

    ok, said = await client.probe("openrouter")

    assert not ok
    assert "refused" not in said, f"a blocked request was called a bad key: {said!r}"
    assert "network" in said or "blocked" in said
    await client.aclose()


async def test_a_401_is_still_reported_as_a_refused_key(settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    client = _client(settings, 401, "invalid api key")

    ok, said = await client.probe("openrouter")

    assert not ok
    assert said == "the key was refused"
    await client.aclose()
