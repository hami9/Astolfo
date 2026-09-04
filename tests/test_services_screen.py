"""Managing services from the panel: adding, ordering, switching off, deleting."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from astolfo import providers as providers_mod
from astolfo import runtime as runtime_mod
from astolfo.admin import on_button, on_text
from astolfo.runtime import Runtime
from tests.conftest import FakeBot, FakeContext, FakeMessage, FakeQuery, make_press, make_update

MASTER = 4242


def _fake_client(settings, registry=None):
    """The real provider selection, with nothing that reaches the network.

    Building the provider list for real is the point: which services end up in it
    is exactly what the panel is changing.
    """
    providers = providers_mod.discover(
        settings.providers,
        fallback_key=settings.api_key,
        stored=registry.rows() if registry else None,
    )
    return SimpleNamespace(
        providers=providers,
        resolve=lambda model, **kwargs: model,
        context_window=lambda model: 0,
        load_catalog=_noop,
        aclose=_noop,
        probe=_probe,
    )


async def _noop():
    return None


async def _probe(name):
    return True, f"answered by {name}/model"


@pytest.fixture
def owned(settings, monkeypatch) -> Runtime:
    monkeypatch.setattr(runtime_mod, "LLMClient", _fake_client)
    monkeypatch.setenv("MASTER_ID", str(MASTER))
    monkeypatch.setenv("DATA_DIR", settings.data_dir)
    return Runtime.build(settings.replace(master_id=MASTER))


async def _press(rt, data: str):
    message = FakeMessage("/panel", chat_id=MASTER, chat_type="private", user_id=MASTER)
    query = FakeQuery(data, message, message.from_user)
    context = FakeContext(rt, FakeBot())
    await on_button(make_press(query), context)
    return query, context


async def _say(rt, context, text: str):
    message = FakeMessage(text, chat_id=MASTER, chat_type="private", user_id=MASTER)
    with pytest.raises(ApplicationHandlerStop):
        await on_text(make_update(message), context)
    return message


# -- the list -------------------------------------------------------------
async def test_the_list_shows_every_service_and_its_state(owned):
    owned.registry.add_key("google", "AIza-key")
    query, _ = await _press(owned, "ap:svc")

    text = query.edits[0]
    assert "google" in text and "openrouter" in text
    assert "no key" in text, "a service without a key says so instead of hiding"


async def test_opening_the_list_costs_no_api_calls(owned):
    """The state on this screen is what the bot learned while it was working."""
    owned.registry.add_key("google", "AIza-key")
    probes: list[str] = []
    owned.llm.probe = lambda name: probes.append(name)

    await _press(owned, "ap:svc")
    await _press(owned, "ap:svc:s:google")

    assert probes == []


# -- custom services ------------------------------------------------------
async def test_a_service_the_code_never_heard_of_can_be_added(owned):
    _query, context = await _press(owned, "ap:svc:new")
    await _say(owned, context, "together https://api.together.xyz/v1 meta-llama/Llama-3.3-70B")

    row = owned.db.service("together")
    assert row["base_url"] == "https://api.together.xyz/v1"
    assert row["models"] == "meta-llama/Llama-3.3-70B"
    assert row["custom"] == 1


async def test_a_custom_service_starts_being_used_once_it_has_a_key(owned):
    _query, context = await _press(owned, "ap:svc:new")
    await _say(owned, context, "together https://api.together.xyz/v1 some/model")
    assert "together" not in [p.name for p in owned.llm.providers], "a service with no key"

    _query, context = await _press(owned, "ap:svc:s:together:addkey")
    await _say(owned, context, "tog-key")

    assert "together" in [p.name for p in owned.llm.providers], "no restart needed"


async def test_nonsense_is_refused_rather_than_stored(owned):
    _query, context = await _press(owned, "ap:svc:new")
    message = await _say(owned, context, "together not-a-url")
    assert "not a URL" in message.sent[0]
    assert owned.db.service("together") is None


async def test_a_built_in_service_cannot_be_overwritten_by_a_new_one(owned):
    _query, context = await _press(owned, "ap:svc:new")
    message = await _say(owned, context, "google https://evil.test/v1")
    assert "built in already" in message.sent[0]
    assert owned.db.service("google") is None


async def test_deleting_a_custom_service_needs_a_second_press(owned):
    owned.registry.add_service("together", "https://api.together.xyz/v1", ["some/model"])
    owned.registry.add_key("together", "tog-key")

    query, _ = await _press(owned, "ap:svc:s:together:del")
    assert "Delete together" in query.edits[0]
    assert owned.db.service("together") is not None

    await _press(owned, "ap:svc:s:together:del!")
    assert owned.db.service("together") is None
    assert owned.db.credentials("together") == [], "its keys go with it"


async def test_a_built_in_service_is_switched_off_not_deleted(owned):
    query, _ = await _press(owned, "ap:svc:s:google:del")
    assert "built in" in (query.answers[0] or "")
    assert owned.db.service("google") is None or owned.db.service("google")["enabled"] == 1


# -- switching off and ordering ------------------------------------------
async def test_a_service_switched_off_is_not_used(owned):
    _query, context = await _press(owned, "ap:svc:s:google:addkey")
    await _say(owned, context, "AIza-key")
    assert "google" in [p.name for p in owned.llm.providers]

    await _press(owned, "ap:svc:s:google:off")

    assert "google" not in [p.name for p in owned.llm.providers]
    assert owned.db.service("google")["enabled"] == 0

    await _press(owned, "ap:svc:s:google:on")
    assert "google" in [p.name for p in owned.llm.providers]


async def test_the_order_can_be_changed_from_the_panel(owned):
    owned.registry.add_key("openrouter", "or-key")
    owned.registry.add_key("google", "AIza-key")
    first = [row["name"] for row in owned.registry.rows()][0]

    await _press(owned, f"ap:svc:s:{'google' if first == 'openrouter' else 'openrouter'}:up")

    assert [row["name"] for row in owned.registry.rows()][0] != first


# -- keys -----------------------------------------------------------------
async def test_a_key_can_be_switched_off_without_removing_it(owned):
    owned.registry.add_key("google", "AIza-one")
    key_id = owned.db.credentials("google")[0]["id"]

    await _press(owned, f"ap:svc:k:{key_id}:off")
    assert owned.db.credential(key_id)["enabled"] == 0

    await _press(owned, f"ap:svc:k:{key_id}:on")
    assert owned.db.credential(key_id)["enabled"] == 1


async def test_waking_a_resting_service_clears_its_rest(owned):
    owned.registry.add_key("google", "AIza-key")
    owned.registry.rest_service("google", 3600, "out of quota")
    key_id = owned.db.credentials("google")[0]["id"]
    owned.registry.rest_credential(key_id, 3600, "refused")

    await _press(owned, "ap:svc:s:google:wake")

    assert owned.db.service("google")["rested_until"] == 0
    assert owned.db.credential(key_id)["rested_until"] == 0


# -- editing --------------------------------------------------------------
async def test_the_models_of_a_service_can_be_edited(owned):
    owned.registry.add_key("google", "AIza-key")
    _query, context = await _press(owned, "ap:svc:s:google:models")
    await _say(owned, context, "gemini-2.0-flash, gemini-2.5-flash")

    assert owned.db.service("google")["models"] == "gemini-2.0-flash,gemini-2.5-flash"


async def test_the_endpoint_of_a_service_can_be_corrected(owned):
    owned.registry.add_key("google", "AIza-key")
    _query, context = await _press(owned, "ap:svc:s:google:url")
    await _say(owned, context, "https://generativelanguage.googleapis.com/v1beta/openai")

    assert owned.db.service("google")["base_url"].endswith("/v1beta/openai")


async def test_every_change_is_written_to_the_audit_trail(owned):
    owned.registry.add_key("google", "AIza-key")
    await _press(owned, "ap:svc:s:google:off")
    assert owned.db.audit_trail()[0]["action"] == "service_off"


# -- choosing a service by hand ------------------------------------------
async def test_a_service_can_be_pinned_and_released(owned):
    _query, context = await _press(owned, "ap:svc:s:google:addkey")
    await _say(owned, context, "AIza-key")

    await _press(owned, "ap:svc:pin:google")
    assert owned.settings.pinned_service == "google", "no restart needed"

    query, _ = await _press(owned, "ap:svc")
    assert "pinned to google" in query.edits[0]

    await _press(owned, "ap:svc:pin:-")
    assert owned.settings.pinned_service == ""


# -- which one is doing best ----------------------------------------------
def _busy(rt, name: str, *, ok: int, failed: int = 0, cost: float = 0.0) -> None:
    """Pretend a service worked today, the way the client records it."""
    for _ in range(ok):
        rt.registry.record_call(name, tokens=100, cost=cost)
    for _ in range(failed):
        rt.registry.record_call(name, failed=True)


async def test_the_ranking_puts_the_reliable_service_first(owned):
    _busy(owned, "openrouter", ok=2, failed=10)
    _busy(owned, "google", ok=12)

    query, _ = await _press(owned, "ap:svc:ranking")
    text = query.edits[0]
    assert text.index("google") < text.index("openrouter")
    assert "100% answered" in text


async def test_a_service_with_barely_any_calls_is_not_judged(owned):
    _busy(owned, "google", ok=1)
    scores = {score.name: score for score in owned.registry.scores()}
    assert "too early to say" in scores["google"].verdict()
    assert scores["google"].value(dearest=0.0) == 0.5


async def test_a_resting_service_is_worth_nothing_right_now(owned):
    _busy(owned, "google", ok=20)
    owned.registry.rest_service("google", 3600, "out of quota")
    scores = {score.name: score for score in owned.registry.scores()}
    assert scores["google"].value(dearest=0.0) == 0.0
    assert scores["google"].verdict() == "resting"


async def test_the_ranking_can_be_applied_to_the_order(owned):
    _busy(owned, "openrouter", ok=2, failed=10)
    _busy(owned, "google", ok=12)
    owned.registry.add_key("google", "AIza-key")

    query, _ = await _press(owned, "ap:svc:reorder")
    assert [row["name"] for row in owned.registry.rows()][0] == "google"
    assert "order is now" in (query.answers[-1] or "")


async def test_reordering_does_nothing_while_a_service_is_pinned(owned):
    owned.settings = owned.settings.replace(pinned_service="openrouter")
    _busy(owned, "openrouter", ok=2, failed=10)
    _busy(owned, "google", ok=12)

    before = [row["name"] for row in owned.registry.rows()]
    query, _ = await _press(owned, "ap:svc:reorder")
    assert [row["name"] for row in owned.registry.rows()] == before
    assert "unpin first" in (query.answers[-1] or "")


async def test_the_ranking_costs_no_api_calls(owned):
    probes: list[str] = []
    owned.llm.probe = lambda name: probes.append(name)
    _busy(owned, "google", ok=12)
    await _press(owned, "ap:svc:ranking")
    assert probes == []
