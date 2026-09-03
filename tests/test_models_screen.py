"""Choosing which model does what, from the panel.

The point of the screen is that a new free model needs no code change and no
restart, so these check the whole way through: catalog to button to setting.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from astolfo import catalog
from astolfo import runtime as runtime_mod
from astolfo.admin import on_button, on_text
from astolfo.llm import Usage
from astolfo.runtime import Runtime
from tests.conftest import FakeBot, FakeContext, FakeMessage, FakeQuery, make_press, make_update

MASTER = 4242

LISTING = [
    {
        "id": f"vendor/model-{index:02d}",
        "name": f"Model {index}",
        "context_length": 200000 - index * 1000,
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    for index in range(10)
] + [
    {
        "id": "vendor/sees-things",
        "name": "Sees Things",
        "context_length": 64000,
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
    },
    {
        "id": "vendor/expensive",
        "name": "Expensive",
        "context_length": 1000000,
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
    },
]


class CatalogLLM:
    """Everything the screen touches, with the catalog already read."""

    def __init__(self, settings, registry=None, listing=LISTING):
        self.providers = [SimpleNamespace(name="openrouter")]
        self.listing = listing
        self.syncs = 0
        self._models = catalog.read(listing)

    def models_offered(self, *, free_only: bool = True, vision: bool = False):
        return [
            m for m in self._models if (m.free or not free_only) and (m.vision or not vision)
        ]

    async def load_catalog(self):
        self.syncs += 1
        self._models = catalog.read(self.listing)

    def resolve(self, model, **kwargs):
        return model

    async def aclose(self):
        return None


@pytest.fixture
def owned(settings, monkeypatch) -> Runtime:
    monkeypatch.setattr(runtime_mod, "LLMClient", CatalogLLM)
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


# -- the overview ---------------------------------------------------------
async def test_the_screen_names_every_job_and_what_it_runs(owned):
    query, _ = await _press(owned, "ap:mdl")

    text = query.edits[0]
    for role in ("fast", "think", "search", "media", "router", "summary"):
        assert role in text
    assert owned.settings.model_fast in text
    assert "11 free chat models" in text, "the paid one is not counted"


def test_the_panel_offers_the_models_screen(owned):
    from astolfo.admin import sections
    from astolfo.admin.panel import Ctx

    view = sections.home(Ctx(rt=owned, user=None, bot=None))
    buttons = [b.callback_data for row in view.markup.inline_keyboard for b in row]
    assert "ap:mdl" in buttons


# -- picking one ----------------------------------------------------------
async def test_the_list_is_paged_and_says_where_you_are(owned):
    query, _ = await _press(owned, "ap:mdl:r:fast:1:1")
    assert "page 1/2" in query.edits[0]

    query, _ = await _press(owned, "ap:mdl:r:fast:2:1")
    assert "page 2/2" in query.edits[0]


async def test_choosing_a_model_stores_it_and_reloads_without_a_restart(owned):
    await _press(owned, "ap:mdl:r:fast:1:1")
    query, _ = await _press(owned, "ap:mdl:set:fast:1:0")

    # The catalog is longest-context first, so index 0 is the widest window.
    assert owned.db.overrides()["model_fast"] == "vendor/model-00"
    assert owned.settings.model_fast == "vendor/model-00", "in effect on the next message"
    assert "vendor/model-00" in query.edits[0]


async def test_each_job_is_set_on_its_own(owned):
    await _press(owned, "ap:mdl:set:fast:1:0")
    await _press(owned, "ap:mdl:set:think:1:1")

    assert owned.settings.model_fast == "vendor/model-00"
    assert owned.settings.model_think == "vendor/model-01"


async def test_the_media_job_only_offers_models_that_can_see(owned):
    query, _ = await _press(owned, "ap:mdl:r:media:1:1")
    assert "sees-things" in query.edits[0]
    assert "model-00" not in query.edits[0], "a text-only model cannot read a photo"

    await _press(owned, "ap:mdl:set:media:1:0")
    assert owned.settings.model_media == "vendor/sees-things"


async def test_paid_models_are_hidden_until_they_are_asked_for(owned):
    query, _ = await _press(owned, "ap:mdl:r:fast:1:1")
    assert "expensive" not in query.edits[0]

    query, _ = await _press(owned, "ap:mdl:r:fast:1:0")
    assert "expensive" in query.edits[0]
    assert "per M" in query.edits[0], "what it charges is on the screen before it is chosen"


async def test_a_model_that_left_the_catalog_does_not_set_anything(owned):
    """The list is rendered from a catalog that can be refreshed under you."""
    query, _ = await _press(owned, "ap:mdl:set:fast:1:999")

    assert "model_fast" not in owned.db.overrides()
    assert "gone from the catalog" in query.answers[0]


# -- search ---------------------------------------------------------------
async def test_a_typed_search_narrows_the_list(owned):
    _query, context = await _press(owned, "ap:mdl:find:fast:1")
    message = await _say(owned, context, "sees")

    assert "sees-things" in message.sent[-1]
    assert "model-00" not in message.sent[-1]


# -- syncing --------------------------------------------------------------
async def test_sync_reads_the_catalog_again(owned):
    before = owned.llm.syncs
    query, _ = await _press(owned, "ap:mdl:sync")

    assert owned.llm.syncs == before + 1
    assert "free of" in query.answers[0]


# -- what the models did --------------------------------------------------
async def test_the_usage_screen_reports_calls_and_tokens_per_model(owned):
    owned.record(
        mode="fast",
        model="vendor/model-00",
        usage=Usage(prompt_tokens=1200, completion_tokens=300, cost=0.0),
    )
    owned.record(
        mode="fast",
        model="vendor/model-00",
        usage=Usage(prompt_tokens=800, completion_tokens=200, cost=0.0),
    )
    owned.record(
        mode="think",
        model="vendor/model-01",
        usage=Usage(prompt_tokens=100, completion_tokens=50, cost=0.002),
    )

    query, _ = await _press(owned, "ap:mdl:u")
    text = query.edits[0]

    assert "3 calls" in text
    assert "model-00" in text and "2 calls" in text
    assert "2.1k in" in text, "tokens are the number that matters on a free model"
    assert "$0.0020" in text


async def test_the_overview_shows_what_the_chosen_model_did(owned):
    await _press(owned, "ap:mdl:set:fast:1:0")
    owned.record(
        mode="fast",
        model="vendor/model-00",
        usage=Usage(prompt_tokens=500, completion_tokens=100),
    )

    query, _ = await _press(owned, "ap:mdl")
    assert "1 calls, 600 tokens" in query.edits[0]


async def test_the_usage_screen_says_so_when_nothing_has_run(owned):
    query, _ = await _press(owned, "ap:mdl:u")
    assert "no model calls" in query.answers[0]
