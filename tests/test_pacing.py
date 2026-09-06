"""The per-minute pacer, and why the bot goes quiet.

Free mode spaces requests out so a per-minute allowance is not blown. At the
default `FREE_RPM=8` that is one model call every 7.5 seconds - the spacing that
shows up in the log - and the allowance is the whole bot's, shared across every
chat.

So the ceiling is not messages per minute, it is *model calls* per minute. A turn
that walks six models before it answers spends 45 seconds of the entire bot's
budget, and every other chat waits. That is what "every few messages it stops
replying" is: not a deadlock, an allowance being spent on calls that were never
going to work.

The lock around the clock was suspected of making it worse by holding services
up behind one another. It does not - a service whose own gap has already elapsed
goes straight through - and the first test here is what says so.
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


async def test_two_services_are_not_made_to_wait_for_each_other(settings):
    client = _client(settings)
    first = client.providers[0]
    second = dataclasses.replace(first, name="somewhere-else")
    # Both are due a full gap, so serialising them would cost two.
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
