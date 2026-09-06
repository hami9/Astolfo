"""The per-minute pacer, and why the bot goes quiet.

Free mode spaces requests out so a per-minute allowance is not blown. At the
default `FREE_RPM=8` that is one model call every 7.5 seconds - the spacing that
shows up in the log - and the allowance is the whole bot's, shared across every
chat.

So the ceiling is not messages per minute, it is *model calls* per minute. A turn
that walks six models before it answers spends 45 seconds of the entire bot's
budget, and every other chat waits.

The clock is per service and the lock now is too. It was one shared lock held
across the sleep, which is head-of-line blocking: a turn bound for a service that
owed nothing waited out a busy service's whole gap. The first version of this
file called that disproved, on a test that had both providers due a full gap -
the one arrangement where the second one's wait has already elapsed by the time
it takes the lock, and so the one arrangement that hides the bug. The test below
is the case that was missing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

import httpx

from astolfo.llm import LLMClient
from tests.test_free_mode import free_settings

# 0.5s per request: long enough to measure, short enough not to slow the suite.
RPM = 120
GAP = 60.0 / RPM


def _client(settings) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    return LLMClient(
        free_settings(settings, free_rpm=RPM), transport=httpx.MockTransport(handler)
    )


async def _elapsed(coros) -> float:
    started = time.monotonic()
    await asyncio.gather(*coros)
    return time.monotonic() - started


async def _own_wait(client, provider) -> float:
    """How long this one provider was held up - not how long the batch took.

    Measuring the batch is what let the first version of this file pass with the
    bug in place: the total is one gap whether the two providers share a lock or
    not. What the shared lock costs is paid by the provider that owed nothing.
    """
    started = time.monotonic()
    await client._pace(provider)
    return time.monotonic() - started


async def test_a_service_that_owes_nothing_does_not_wait_for_one_that_does(settings):
    """The case the first version of this test missed."""
    client = _client(settings)
    busy = client.providers[0]
    idle = dataclasses.replace(busy, name="somewhere-else")
    busy.last_request = time.monotonic()       # owes a full gap
    idle.last_request = time.monotonic() - 99  # owes nothing at all

    # The busy one goes first, so with a shared lock the idle one pays its gap.
    _, idle_waited = await asyncio.gather(
        client._pace(busy), _own_wait(client, idle)
    )

    assert idle_waited < GAP / 2, (
        f"a service that owed nothing waited {idle_waited:.2f}s for one that did"
    )
    await client.aclose()


async def test_two_services_both_due_a_gap_still_do_not_add_up(settings):
    client = _client(settings)
    first = client.providers[0]
    second = dataclasses.replace(first, name="somewhere-else")
    now = time.monotonic()
    first.last_request = now
    second.last_request = now

    took = await _elapsed([client._pace(first), client._pace(second)])

    assert took < GAP * 1.6, f"the services queued behind one another: {took:.2f}s"
    await client.aclose()


async def test_one_service_still_spaces_its_own_requests_out(settings):
    """The pacer's actual job, which the fix must not give away."""
    client = _client(settings)
    provider = client.providers[0]
    provider.last_request = time.monotonic()

    took = await _elapsed([client._pace(provider), client._pace(provider)])

    assert took >= GAP * 1.6, f"two requests to one service went too close together: {took:.2f}s"
    await client.aclose()
