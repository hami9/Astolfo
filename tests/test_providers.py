"""Stacking several services, each with its own free allowance."""

from __future__ import annotations

import json

import httpx

from astolfo import providers as providers_mod
from astolfo.llm import LLMClient


def _settings(settings, **overrides):
    return settings.replace(**{"free_mode": True, "free_rpm": 0, **overrides})


# -- configuration --------------------------------------------------------
def test_only_services_with_a_key_are_used(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("GROQ_API_KEY", "gq")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    found = providers_mod.discover(["openrouter", "google", "groq"])
    assert [p.name for p in found] == ["openrouter", "groq"]


def test_order_is_respected(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("GROQ_API_KEY", "gq")

    found = providers_mod.discover(["groq", "openrouter"])
    assert [p.name for p in found] == ["groq", "openrouter"]


def test_endpoint_and_models_are_overridable(monkeypatch):
    """A service changing its URL should be a config edit, not a release."""
    monkeypatch.setenv("GROQ_API_KEY", "gq")
    monkeypatch.setenv("GROQ_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GROQ_MODELS", "a/one, a/two")

    provider = providers_mod.discover(["groq"])[0]
    assert provider.chat_url == "https://example.test/v1/chat/completions"
    assert provider.models == ["a/one", "a/two"]


def test_unknown_names_are_reported_not_swallowed():
    assert providers_mod.unknown_names(["openrouter", "wat"]) == ["wat"]


def test_a_lone_key_still_works(settings, monkeypatch):
    """The single-service setup must keep working untouched."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = LLMClient(settings)
    assert [p.name for p in client.providers] == ["openrouter"]


# -- failover -------------------------------------------------------------
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


# Every service is asked for its own listing now, so each has to answer with
# something it would plausibly say. Google returning OpenRouter's catalog would
# have it adopt OpenRouter's ids, which is correct behaviour on absurd input.
GOOGLE_LISTING = {
    "data": [
        {"id": "gemini-flash-latest"},
        {"id": "gemini-flash-lite-latest"},
        {"id": "gemini-pro-latest"},
    ]
}


def _listing_for(host: str) -> dict:
    return CATALOG if "openrouter" in host else GOOGLE_LISTING


def _router(status_by_host: dict[str, int], seen: list[tuple[str, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=_listing_for(host))
        model = json.loads(request.content)["model"]
        seen.append((host, model))
        status = status_by_host.get(host, 200)
        if status != 200:
            return httpx.Response(status, text="no allowance left")
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": "ehehe~"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    return handler


def _multi(settings, monkeypatch, handler, order=("openrouter", "google")):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    cfg = _settings(settings, providers=list(order))
    return LLMClient(cfg, transport=httpx.MockTransport(handler))


async def test_a_spent_service_hands_over_to_the_next(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok, "the second service answered"
    hosts = [host for host, _ in seen]
    assert hosts[0] == "openrouter.ai"
    assert "generativelanguage.googleapis.com" in hosts


async def test_each_service_is_asked_for_its_own_models(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    by_host = dict(seen)
    assert by_host["openrouter.ai"] == "free/one", "discovered from its catalog"
    assert by_host["generativelanguage.googleapis.com"].startswith("gemini")


async def test_a_rested_service_is_skipped_next_time(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="anything")
    seen.clear()
    await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert all(host != "openrouter.ai" for host, _ in seen), "no point asking again"


async def test_exhausting_everything_is_reported_as_throttled(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    everything = {"openrouter.ai": 429, "generativelanguage.googleapis.com": 429}
    client = _multi(settings, monkeypatch, _router(everything, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind == "throttled"
    assert client.throttled_for() > 0
    assert len({host for host, _ in seen}) == 2, "both were tried before giving up"


async def test_a_rejected_request_is_offered_to_the_next_service(settings, monkeypatch):
    """Services disagree about which fields they accept, so a 400 is not the end."""
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 400}, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok, "the service that understood the request answered"
    assert [host for host, _ in seen][0] == "openrouter.ai"
    assert "generativelanguage.googleapis.com" in [host for host, _ in seen]


async def test_a_rejected_request_does_not_rest_the_service(settings, monkeypatch):
    """A malformed request says nothing about the allowance, so nothing is paused."""
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 400}, seen))
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "one"}], model="anything")
    seen.clear()
    await client.chat([{"role": "user", "content": "two"}], model="anything")

    assert seen[0][0] == "openrouter.ai", "still first in line"


async def test_a_request_nobody_accepts_ends_the_turn(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    everywhere = {"openrouter.ai": 400, "generativelanguage.googleapis.com": 400}
    client = _multi(settings, monkeypatch, _router(everywhere, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind == "rejected"
    assert len({host for host, _ in seen}) == 2, "both were asked before giving up"


# -- request shape --------------------------------------------------------
OPENROUTER_ONLY = ("models", "plugins", "provider", "usage", "reasoning")


def _recorder(bodies: dict[str, dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOG)
        body = json.loads(request.content)
        bodies[request.url.host] = body
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": "ehehe~"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    return handler


async def test_plain_services_are_sent_a_plain_request(settings, monkeypatch):
    """OpenRouter's extras are rejected outright by an ordinary OpenAI endpoint."""
    bodies: dict[str, dict] = {}
    client = _multi(settings, monkeypatch, _recorder(bodies), order=("google",))
    await client.load_catalog()

    await client.chat(
        [{"role": "user", "content": "hi"}],
        model="anything",
        reasoning={"effort": "low"},
        web=True,
    )

    body = bodies["generativelanguage.googleapis.com"]
    assert [key for key in OPENROUTER_ONLY if key in body] == []
    assert body["messages"] and body["model"]


async def test_openrouter_still_gets_its_extras(settings, monkeypatch):
    bodies: dict[str, dict] = {}
    client = _multi(settings, monkeypatch, _recorder(bodies), order=("openrouter",))
    await client.load_catalog()

    await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert bodies["openrouter.ai"].get("usage") == {"include": True}


async def test_a_refused_key_steps_aside_for_the_next_service(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 401}, seen))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "one"}], model="anything")
    assert result.ok, "the service with a working key answered"

    seen.clear()
    await client.chat([{"role": "user", "content": "two"}], model="anything")
    assert all(host != "openrouter.ai" for host, _ in seen), "a bad key stays bad"


# -- model ids ------------------------------------------------------------
def _listing(models_by_host: dict[str, list[str]], seen: list[tuple[str, str]]):
    """A service that publishes its own model list and 404s on anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        offered = models_by_host.get(host)
        if request.url.path.endswith("/models"):
            if offered is None:
                return httpx.Response(200, json=_listing_for(host))
            return httpx.Response(200, json={"data": [{"id": m} for m in offered]})

        model = json.loads(request.content)["model"]
        seen.append((host, model))
        if offered is not None and model not in offered:
            return httpx.Response(404, json={"error": {"message": f"{model} is not found"}})
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": "ehehe~"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    return handler


async def test_models_a_service_no_longer_offers_are_dropped(settings, monkeypatch):
    """Preset ids go stale; the service's own listing is the authority."""
    seen: list[tuple[str, str]] = []
    listing = {"generativelanguage.googleapis.com": ["gemini-flash-lite-latest"]}
    client = _multi(settings, monkeypatch, _listing(listing, seen), order=("google",))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert result.ok
    assert [model for _, model in seen] == ["gemini-flash-lite-latest"], "retired ids never asked"


async def test_a_service_that_lists_nothing_useful_keeps_its_configured_ids(
    settings, monkeypatch
):
    """An unreadable or unrelated listing must not leave the service with no models."""
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({}, seen), order=("google",))
    await client.load_catalog()

    provider = client.providers[0]
    assert provider.models, "still usable"


async def test_a_missing_model_moves_down_the_list(settings, monkeypatch):
    """404 means this id is gone, not that the service is unusable."""
    seen: list[tuple[str, str]] = []
    handler = _listing({"generativelanguage.googleapis.com": []}, seen)

    def stale(request: httpx.Request) -> httpx.Response:
        # The listing agrees with the preset, but a model 404s when called.
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GOOGLE_LISTING)
        return handler(request)

    client = _multi(settings, monkeypatch, stale, order=("google",))
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert result.error_kind == "rejected"
    tried = [model for _, model in seen]
    assert len(tried) == len(set(tried)) >= 2, "every configured id was tried once"


# -- the built-in list ----------------------------------------------------
def test_every_preset_is_usable_as_written():
    """A preset nobody can reach is worse than no preset."""
    for name, preset in providers_mod.PRESETS.items():
        assert preset.name == name
        assert preset.base_url.startswith("https://"), name
        assert not preset.base_url.endswith("/"), name
        assert preset.key_env.isupper(), name
        # Only the catalog service may ship without model names.
        assert preset.models or preset.discovers_free_models, name
        assert set(preset.vision_models) <= set(preset.models) or preset.vision_models, name


def test_a_preset_needs_no_edit_to_PROVIDERS(settings, monkeypatch):
    """Giving a service a key is the whole setup; the order is a panel matter."""
    monkeypatch.setenv("MISTRAL_API_KEY", "ms-key")
    found = providers_mod.discover(["mistral"])
    assert [p.name for p in found] == ["mistral"]
    assert found[0].base_url == providers_mod.PRESETS["mistral"].base_url
    assert found[0].models == providers_mod.PRESETS["mistral"].models


def test_a_service_stored_by_the_panel_joins_without_touching_providers():
    stored = [{"name": "sambanova", "credentials": [{"id": 1, "value": "sn-key"}]}]
    found = providers_mod.discover(["openrouter"], fallback_key="or", stored=stored)
    assert "sambanova" in [p.name for p in found]


# -- choosing by hand -----------------------------------------------------
async def test_pinning_a_service_stops_the_failover(settings, monkeypatch):
    """Manual means manual: the pinned one answers or the turn fails."""
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    client._s = client._s.replace(pinned_service="openrouter")
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")

    assert not result.ok
    assert {host for host, _ in seen} == {"openrouter.ai"}, "google was never asked"


async def test_unpinning_restores_the_order(settings, monkeypatch):
    seen: list[tuple[str, str]] = []
    client = _multi(settings, monkeypatch, _router({"openrouter.ai": 429}, seen))
    client._s = client._s.replace(pinned_service="")
    await client.load_catalog()

    result = await client.chat([{"role": "user", "content": "hi"}], model="anything")
    assert result.ok


def test_deepseek_and_openai_are_known_out_of_the_box():
    """Both are plain OpenAI-compatible endpoints, so nothing else is needed."""
    for name in ("deepseek", "openai"):
        preset = providers_mod.PRESETS[name]
        assert preset.models, "a service with no models can never be called"
        assert preset.base_url.startswith("https://")
        assert not preset.openrouter_extensions, "only OpenRouter takes OpenRouter's fields"
        assert preset.signup and preset.note, "the panel shows what it costs before you use it"


def test_deepseek_is_not_offered_as_a_way_to_read_pictures():
    """Its chat models are text only; offering them for media drops attachments."""
    assert providers_mod.PRESETS["deepseek"].vision_models == []
    assert providers_mod.PRESETS["openai"].vision_models


# -- discovery across every service ---------------------------------------
GROQ_LISTING = {
    "data": [
        {"id": "llama-4-scout-17b-16e-instruct", "context_window": 131072},
        {"id": "qwen3-32b", "context_window": 131072},
        {"id": "whisper-large-v3"},
    ]
}


def _everyone(seen: list[str]):
    """Each service answers with its own listing, the way each really shapes it."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            seen.append(host)
            if "openrouter" in host:
                return httpx.Response(200, json=CATALOG)
            if "groq" in host:
                return httpx.Response(200, json=GROQ_LISTING)
            return httpx.Response(200, json=GOOGLE_LISTING)
        return httpx.Response(
            200,
            json={
                "model": json.loads(request.content)["model"],
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    return handler


def _three(settings, monkeypatch, handler, **overrides):
    for env in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
        monkeypatch.setenv(env, "k")
    cfg = _settings(settings, providers=["openrouter", "google", "groq"], **overrides)
    return LLMClient(cfg, transport=httpx.MockTransport(handler))


async def test_every_service_is_asked_what_it_offers(settings, monkeypatch):
    """Only one listing was ever read, so twelve services were invisible."""
    asked: list[str] = []
    client = _three(settings, monkeypatch, _everyone(asked))
    await client.load_catalog()

    assert len(asked) == 3, "each service, once"
    services = {m.service for m in client.known_models()}
    assert services == {"openrouter", "google", "groq"}


async def test_a_new_model_on_a_bare_service_becomes_selectable(settings, monkeypatch):
    client = _three(settings, monkeypatch, _everyone([]))
    await client.load_catalog()

    groq = {m.id: m for m in client.models_offered(free_only=False, service="groq")}
    assert "qwen3-32b" in groq, "adopted from Groq's own listing"
    assert "whisper-large-v3" not in groq, "and the speech model is not a chat model"
    assert groq["llama-4-scout-17b-16e-instruct"].vision, "read from the name"


async def test_a_window_is_known_for_every_service_not_just_one(settings, monkeypatch):
    """context_window returned 0 for twelve services, so history was never trimmed."""
    client = _three(settings, monkeypatch, _everyone([]))
    await client.load_catalog()

    assert client.context_window("qwen3-32b", "groq") == 131072, "Groq states it"
    assert client.context_window("gemini-flash-latest", "google") > 0, "guessed from the name"
    assert client.context_window("free/one", "openrouter") == 100000


async def test_one_service_is_never_offered_another_s_model(settings, monkeypatch):
    """The pool is global now, and asking OpenRouter for a Gemini id is a 404."""
    client = _three(settings, monkeypatch, _everyone([]))
    await client.load_catalog()

    for name in ("openrouter", "google", "groq"):
        provider = next(p for p in client.providers if p.name == name)
        mine = {m.id for m in client.models_offered(free_only=False, service=name)}
        assert set(client.free_pool(service=name)) <= mine, name
        assert client._candidates(provider, "anything")[0] in mine | {"anything"}


async def test_a_service_that_renamed_everything_heals_itself(settings, monkeypatch):
    """A stale list used to answer 404 on every message, forever."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gemini-9-turbo"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    cfg = _settings(settings, providers=["google"], free_mode=False)
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    await client.load_catalog()

    assert client.providers[0].models == ["gemini-9-turbo"]


async def test_a_service_that_will_not_say_keeps_what_it_was_given(settings, monkeypatch):
    def silent(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(500, text="nope")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    cfg = _settings(settings, providers=["google"], free_mode=False)
    client = LLMClient(cfg, transport=httpx.MockTransport(silent))
    await client.load_catalog()

    assert client.providers[0].models == list(providers_mod.PRESETS["google"].models)


async def test_the_adopted_list_stays_short(settings, monkeypatch):
    """It is walked on failover, so a service listing hundreds cannot become the chain."""
    many = {"data": [{"id": f"model-{i}"} for i in range(50)]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=many)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    cfg = _settings(settings, providers=["google"], free_mode=False)
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    await client.load_catalog()

    from astolfo.llm import ADOPT_AT_MOST

    assert len(client.providers[0].models) == ADOPT_AT_MOST
    assert len(client.models_offered(free_only=False, service="google")) == 50, "all still known"


# -- remembering what was already there -----------------------------------
async def test_the_catalog_is_remembered_so_new_means_new(settings, monkeypatch):
    """Held in the database, or every model would look new after a restart."""
    from astolfo.crypto import SecretBox
    from astolfo.db import open_database
    from astolfo.services import ServiceRegistry

    database = open_database(settings.data_dir)
    registry = ServiceRegistry(database, SecretBox(settings.data_dir))
    for env in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
        monkeypatch.setenv(env, "k")
    cfg = _settings(settings, providers=["openrouter", "google", "groq"])

    client = LLMClient(cfg, transport=httpx.MockTransport(_everyone([])), registry=registry)
    await client.load_catalog()
    remembered = {(row["service"], row["model"]) for row in database.newest_models(limit=100)}
    assert ("groq", "qwen3-32b") in remembered
    assert ("groq", "whisper-large-v3") not in remembered, "not a chat model"
    assert {service for service, _ in remembered} == {"openrouter", "google", "groq"}

    # A second read, in a second process, finds nothing new.
    again = LLMClient(cfg, transport=httpx.MockTransport(_everyone([])), registry=registry)
    await again.load_catalog()
    assert registry.note_models([("groq", "qwen3-32b", 1, True, False)]) == []


async def test_a_catalog_that_cannot_be_recorded_does_not_lose_the_catalog(
    settings, monkeypatch
):
    """The memory is a convenience; the models themselves are the point."""

    class Broken:
        def rows(self):
            return None

        def note_models(self, seen):
            raise RuntimeError("disk is full")

    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    cfg = _settings(settings, providers=["google"])
    client = LLMClient(cfg, transport=httpx.MockTransport(_everyone([])), registry=Broken())
    await client.load_catalog()

    assert client.models_offered(free_only=False, service="google")
