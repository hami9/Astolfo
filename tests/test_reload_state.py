"""What a settings reload has to carry, and what it has to leave running.

Two things from one production diagnostics, both of which read as the bot
disagreeing with itself:

    settings:  brain           on (writing on)
    brain:     selecting  off

and, thirty-three seconds after a reload,

    RuntimeError: Cannot send a request, as the client has been closed
    no completion for chat ...
"""

from __future__ import annotations

import asyncio

import httpx

from astolfo.llm import LLMClient
from astolfo.runtime import Runtime


def _settings(settings, **overrides):
    return settings.replace(providers=["openrouter"], free_mode=True, **overrides)


# -- 1. the switch the reload forgot --------------------------------------
async def test_turning_the_brain_on_from_the_panel_actually_turns_it_on(settings, llm):
    """`brain.on` was set once, in __post_init__, and a reload never touched it.

    Every surface read `settings.brain` and said "on"; the one place that decides
    reads `brain.on` and returned the factory recipe. The switch did nothing
    until the service was restarted.
    """
    rt = Runtime.build(_settings(settings, brain=False))
    rt.llm = llm
    assert rt.brain.on is False

    await rt.reconfigure(_settings(settings, brain=True))

    assert rt.brain.on is True, "the panel switch did not reach the brain"


async def test_turning_it_off_again_also_reaches_the_brain(settings, llm):
    rt = Runtime.build(_settings(settings, brain=True))
    rt.llm = llm
    assert rt.brain.on is True

    await rt.reconfigure(_settings(settings, brain=False))

    assert rt.brain.on is False


async def test_a_reload_keeps_what_the_brain_has_learned(settings, llm):
    """A switch, not a reset - the counters survive a reload as they survive a
    restart, because they are what make turning it on not start from nothing."""
    rt = Runtime.build(_settings(settings, brain=False))
    rt.llm = llm
    from astolfo import recipes

    rt.brain.note(
        model="qwen3-32b", recipe=recipes.FACTORY_TIGHT, free_mode=True,
        answered=True, chars=120,
    )
    before = rt.brain.seen("qwen3")
    assert before

    await rt.reconfigure(_settings(settings, brain=True))

    assert rt.brain.seen("qwen3") == before


async def test_a_reload_leaves_the_runtime_where_a_restart_would(settings, llm):
    """The general shape of the bug, rather than the one field that had it.

    __post_init__ and reconfigure are two ways to reach the same state, and
    anything derived from settings in one has to be derived in the other. This is
    what would have caught `brain.on` without knowing to look for it.
    """
    changed = _settings(settings, brain=True, attention_hold=123, response_cache_ttl=77)

    reloaded = Runtime.build(_settings(settings, brain=False))
    reloaded.llm = llm
    await reloaded.reconfigure(changed)

    fresh = Runtime.build(changed)
    fresh.llm = llm

    assert reloaded.brain.on == fresh.brain.on
    assert reloaded.settings.brain == fresh.settings.brain
    assert reloaded.attention.hold == fresh.attention.hold


# -- 2. the drain that let go between two attempts ------------------------
async def test_a_turn_sleeping_before_its_retry_survives_a_reload(settings, monkeypatch):
    """The production traceback.

    `_in_flight` wrapped only the POST, so between two attempts of one turn the
    count was zero: the drain stopped waiting, closed the pools, and the retry
    posted to a closed client. The window is a turn that failed once and was
    backing off - which is the turn that most needs the client to live.
    """
    attempts = 0
    first_failed = asyncio.Event()

    async def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        attempts += 1
        if attempts == 1:
            first_failed.set()
            return httpx.Response(500, json={"error": {"message": "upstream error"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(_settings(settings), transport=httpx.MockTransport(flaky))

    turn = asyncio.create_task(client.chat([{"role": "user", "content": "hi"}], model="m"))
    await first_failed.wait()
    # The reload lands while the turn is asleep between attempt one and two.
    closing = asyncio.create_task(client.aclose())

    result = await asyncio.wait_for(turn, timeout=30.0)

    assert result.ok, f"the retry died with the client closed under it: {result.error}"
    assert attempts >= 2, "the turn should have retried"
    await asyncio.wait_for(closing, timeout=30.0)


async def test_the_drain_does_not_close_in_the_handover_between_services(settings, monkeypatch):
    """The gap v2.8.3 left, still failing on the box it shipped to.

        14:48:30 | no completion for chat ...: Cannot send a request, as the
                   client has been closed.

    `chat` walks the provider list calling `_chat_with` once per service, so the
    marker is taken and released *per service*. When the first one releases,
    `_idle` is set and `aclose`'s waiter is woken; `chat` then continues
    synchronously into the next service and takes the marker again - but `aclose`
    waits on the event once and never re-checks, so it closes the pools under a
    turn that is still running.

    Driven at the marker rather than through two live services, because the
    handover is synchronous: the loop cannot be made to interleave there on
    purpose, which is exactly why the first version of this test passed.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(
        _settings(settings),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        ),
    )

    async def turn() -> None:
        async with client._in_flight():          # the first service
            await asyncio.sleep(0.05)
        async with client._in_flight():          # and straight on to the next
            await asyncio.sleep(0.05)

    running = asyncio.create_task(turn())
    await asyncio.sleep(0.01)
    closing = asyncio.create_task(client.aclose())
    await asyncio.wait_for(running, timeout=10.0)

    result = await client.chat([{"role": "user", "content": "hi"}], model="m")
    assert result.ok, f"the pools were closed during the handover: {result.error}"
    await asyncio.wait_for(closing, timeout=10.0)


# -- 3. and what it says when there is genuinely nothing left --------------
async def test_the_resting_message_names_the_soonest_return(settings, monkeypatch):
    """It reported the longest wait, so one service resting a day made the bot
    say nothing would answer for a day - while another was a minute away."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    client = LLMClient(
        settings.replace(providers=["openrouter", "google"], free_mode=True),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": []})
        ),
    )
    now = __import__("time").monotonic()
    client.providers[0].paused_until = now + 86400  # a day, from a 403
    client.providers[1].paused_until = now + 60     # a minute, from a 429

    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert not result.ok
    assert "86" not in (result.error or ""), f"it quoted the longest wait: {result.error}"
    assert "60s" in (result.error or "") or "59s" in (result.error or "")
    await client.aclose()
