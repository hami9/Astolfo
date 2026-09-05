"""A model retired for the rest of the day, and asked again anyway.

Two faults behind one number from the server: 49 calls to
`cohere/command-r-08-2024`, 21 of them unusable, 23 strikes against it - and
not one call to `command-r7b-12-2024`, the other model that service names.
The escalating rest v2.5.3 gave a broken model was being spent nowhere.
"""

from __future__ import annotations

import time

import httpx

from astolfo.llm import EMPTY_STRIKES, LLMClient


def _ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": []})
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


def _client(settings, registry=None, service="cohere"):
    return LLMClient(
        settings.replace(providers=[service], free_mode=True),
        transport=httpx.MockTransport(_ok),
        registry=registry,
    )


def _registry(settings):
    from astolfo.crypto import SecretBox
    from astolfo.db import open_database
    from astolfo.services import ServiceRegistry

    return ServiceRegistry(open_database(settings.data_dir), SecretBox(settings.data_dir))


# -- 1. a service that names its own models never honoured a rest ----------
def test_a_rested_model_is_not_the_one_asked_next(settings, monkeypatch):
    """Only the discovered pool checked the cooldown, so at every other service
    the retry after an unusable reply asked the same model again."""
    monkeypatch.setenv("COHERE_API_KEY", "k")
    client = _client(settings)
    provider = client.providers[0]
    assert provider.models[:2] == ["command-r-08-2024", "command-r7b-12-2024"]

    assert client._pick_model(provider, "command-r-08-2024") == "command-r-08-2024"
    client.mark_unusable("command-r-08-2024")

    assert client._pick_model(provider, "command-r-08-2024") == "command-r7b-12-2024"


def test_a_service_whose_models_are_all_resting_still_answers(settings, monkeypatch):
    """Sunk, never silent: a turn with nothing left to try is worse than a turn
    with a poor model."""
    monkeypatch.setenv("COHERE_API_KEY", "k")
    client = _client(settings)
    provider = client.providers[0]
    for model in provider.models:
        client.mark_unusable(model)

    assert client._pick_model(provider, "command-r-08-2024") in provider.models


def test_the_worse_of_two_named_models_is_asked_second(settings, monkeypatch):
    """Once both are awake again the record still orders them."""
    monkeypatch.setenv("COHERE_API_KEY", "k")
    client = _client(settings)
    provider = client.providers[0]
    client.mark_unusable("command-r-08-2024", seconds=0.0)

    assert client._candidates(provider, "command-r-08-2024") == [
        "command-r7b-12-2024",
        "command-r-08-2024",
    ]


# -- 2. and the rest itself did not survive a restart ----------------------
def test_a_rest_outlives_the_update_that_earned_it(settings, monkeypatch):
    """The strike count was written down and the rest was not, so a model
    retired for twelve hours was back in the pool minutes later - and the log
    showed three restarts in the day it was measured."""
    monkeypatch.setenv("COHERE_API_KEY", "k")
    registry = _registry(settings)

    before = _client(settings, registry)
    before.mark_unusable("command-r-08-2024")
    rest = before._cooldowns["command-r-08-2024"] - time.monotonic()
    assert rest > 0

    after = _client(settings, registry)
    carried = after._cooldowns.get("command-r-08-2024", 0.0) - time.monotonic()

    assert carried > 0, "it is still resting"
    assert abs(carried - rest) < 5, "for what was left of it, not from the start"
    assert after._pick_model(after.providers[0], "command-r-08-2024") == "command-r7b-12-2024"


def test_a_third_strike_still_costs_the_rest_of_the_day_after_a_restart(settings, monkeypatch):
    """The strikes carry, so the rest they earn has to carry with them."""
    monkeypatch.setenv("COHERE_API_KEY", "k")
    registry = _registry(settings)

    for _ in range(2):
        _client(settings, registry).mark_unusable("command-r-08-2024")
    third = _client(settings, registry)
    third.mark_unusable("command-r-08-2024")

    earned = third._cooldowns["command-r-08-2024"] - time.monotonic()
    assert earned > EMPTY_STRIKES[1], "the third offence, not the first"


def test_an_expired_rest_is_not_carried(settings, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "k")
    registry = _registry(settings)
    registry.rest_model("command-r-08-2024", 60.0)
    registry._db.execute(
        "UPDATE model_health SET rested_until = ? WHERE model = ?",
        (time.time() - 1, "command-r-08-2024"),
    )

    assert "command-r-08-2024" not in _client(settings, registry)._cooldowns


def test_forgetting_a_models_record_wakes_it_too(settings, monkeypatch):
    """The panel button says a clean sheet, and a rest is part of the sheet."""
    monkeypatch.setenv("COHERE_API_KEY", "k")
    registry = _registry(settings)
    _client(settings, registry).mark_unusable("command-r-08-2024")
    registry._db.forget_model_health()

    assert _client(settings, registry)._cooldowns == {}


def test_a_rest_is_never_recorded_for_a_model_with_no_record(settings):
    """`rest_model` updates the row `note_strike` inserted; it invents nothing."""
    registry = _registry(settings)
    registry.rest_model("never/heard-of-it", 60.0)

    assert registry.model_rests() == {}


def test_recording_a_rest_never_breaks_a_turn(settings, monkeypatch):
    class _Broken:
        def rows(self):
            return None

        def model_strikes(self):
            return {}

        def model_rests(self):
            return {}

        def note_strike(self, model):
            return 1

        def rest_model(self, model, seconds):
            raise RuntimeError("disk is full")

    monkeypatch.setenv("COHERE_API_KEY", "k")
    client = _client(settings, _Broken())
    client.mark_unusable("command-r-08-2024")

    assert client._cooldowns["command-r-08-2024"] > time.monotonic(), "it still rests in memory"
