"""More than one key for a service: which one is used, and when it steps aside."""

from __future__ import annotations

import json

import httpx
import pytest

from astolfo.crypto import SecretBox
from astolfo.db import open_database
from astolfo.llm import AUTH_COOLDOWN, LLMClient
from astolfo.services import ServiceRegistry

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


@pytest.fixture
def registry(settings) -> ServiceRegistry:
    return ServiceRegistry(open_database(settings.data_dir), SecretBox(settings.data_dir))


def _router(status_by_key: dict[str, int], seen: list[str]):
    """Answers according to which key the request carried."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        seen.append(key)
        status = status_by_key.get(key, 200)
        if status != 200:
            return httpx.Response(status, text="nope")
        return httpx.Response(
            200,
            json={
                "model": json.loads(request.content)["model"],
                "choices": [{"message": {"content": "ehehe~"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.0},
            },
        )

    return handler


def _client(settings, registry, handler, **overrides) -> LLMClient:
    live = settings.replace(
        free_mode=True, free_rpm=0, providers=["openrouter"], **overrides
    )
    return LLMClient(live, transport=httpx.MockTransport(handler), registry=registry)


# -- which key is used ----------------------------------------------------
async def test_the_first_working_key_is_the_one_used(settings, registry):
    registry.add_key("openrouter", "key-one", label="first")
    registry.add_key("openrouter", "key-two", label="second")
    seen: list[str] = []

    client = _client(settings, registry, _router({}, seen))
    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert seen == ["key-one"], "the spare is a spare, not a rotation"


async def test_a_refused_key_hands_over_to_the_next(settings, registry):
    """Replacing a key should never mean a gap in service."""
    registry.add_key("openrouter", "expired", label="old")
    registry.add_key("openrouter", "fresh", label="new")
    seen: list[str] = []

    client = _client(settings, registry, _router({"expired": 401}, seen))
    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok
    assert seen == ["expired", "fresh"]


async def test_a_refused_key_is_remembered_and_not_tried_again(settings, registry):
    first = registry.add_key("openrouter", "expired")
    registry.add_key("openrouter", "fresh")
    seen: list[str] = []

    client = _client(settings, registry, _router({"expired": 401}, seen))
    await client.chat([{"role": "user", "content": "one"}], model="anything")
    seen.clear()
    await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert seen == ["fresh"]
    row = registry._db.credential(first)
    assert row["rested_until"] > 0
    assert "refused" in row["last_error"]
    assert row["failures"] == 1


async def test_every_key_refused_ends_the_turn(settings, registry, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    registry.add_key("openrouter", "one")
    registry.add_key("openrouter", "two")
    seen: list[str] = []

    client = _client(settings, registry, _router({"one": 401, "two": 401}, seen), api_key="")
    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind == "auth"
    assert sorted(seen) == ["one", "two"], "both were tried before giving up"


async def test_a_disabled_key_is_never_sent(settings, registry):
    disabled = registry.add_key("openrouter", "paused")
    registry.add_key("openrouter", "live")
    registry.set_key_enabled(disabled, False)
    seen: list[str] = []

    client = _client(settings, registry, _router({}, seen))
    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert seen == ["live"]


# -- what survives a restart ---------------------------------------------
async def test_a_service_out_of_allowance_is_still_resting_after_a_restart(settings, registry):
    registry.add_key("openrouter", "key")
    seen: list[str] = []

    client = _client(settings, registry, _router({"key": 429}, seen))
    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")
    assert result.error_kind == "throttled"

    seen.clear()
    restarted = _client(settings, registry, _router({"key": 429}, seen))
    result = await restarted.chat([{"role": "user", "content": "hi"}], model="anything")

    assert seen == [], "a quota that runs until tomorrow is still spent tomorrow"
    assert result.error_kind == "throttled"


async def test_waking_a_service_clears_the_rest(settings, registry):
    registry.add_key("openrouter", "key")
    seen: list[str] = []
    client = _client(settings, registry, _router({"key": 429}, seen))
    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    registry.wake("openrouter")
    seen.clear()
    awake = _client(settings, registry, _router({}, seen))
    result = await awake.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok
    assert seen == ["key"]


# -- accounting -----------------------------------------------------------
async def test_a_call_is_booked_against_its_service_and_its_key(settings, registry):
    key = registry.add_key("openrouter", "key")
    client = _client(settings, registry, _router({}, []))

    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    usage = registry.usage_today()["openrouter"]
    assert usage["requests"] == 1
    assert usage["tokens"] == 5
    assert registry._db.credential(key)["requests"] == 1
    assert registry._db.credential(key)["last_ok"] > 0


# -- the environment still works -----------------------------------------
async def test_a_key_in_the_env_alone_still_runs_the_bot(settings, registry, monkeypatch):
    """Nobody who has never opened the panel should notice any of this."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    seen: list[str] = []

    client = _client(settings, registry, _router({}, seen))
    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok
    assert seen == ["from-env"]


async def test_a_stored_key_is_preferred_over_the_env_one(settings, registry, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    registry.add_key("openrouter", "from-panel")
    seen: list[str] = []

    client = _client(settings, registry, _router({}, seen))
    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert seen == ["from-panel"], "the newer intent wins"


async def test_the_env_key_is_the_spare_when_the_stored_one_is_refused(
    settings, registry, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    registry.add_key("openrouter", "from-panel")
    seen: list[str] = []

    client = _client(settings, registry, _router({"from-panel": 401}, seen))
    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok
    assert seen == ["from-panel", "from-env"]


def test_a_refused_env_key_has_nothing_to_write_to(settings, registry):
    """It has no row, so resting it must not blow up."""
    registry.rest_credential(None, AUTH_COOLDOWN, "refused")
    registry.note_use(None)
    assert registry._db.credentials() == []
