"""Adopting changed settings without a restart."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astolfo import runtime as runtime_mod
from astolfo.runtime import Runtime


class _Client:
    """Stands in for LLMClient so reconfiguring touches no network."""

    instances: list[_Client] = []

    def __init__(self, settings, registry=None) -> None:
        self.settings = settings
        self.registry = registry
        self.closed = False
        self.catalog_loads = 0
        self.providers = [SimpleNamespace(name="openrouter")]
        _Client.instances.append(self)

    async def load_catalog(self) -> None:
        self.catalog_loads += 1

    async def aclose(self) -> None:
        self.closed = True


async def test_reconfiguring_reaches_every_part_of_the_bot(settings, monkeypatch):
    monkeypatch.setattr(runtime_mod, "LLMClient", _Client)
    _Client.instances.clear()
    rt = Runtime.build(settings)
    first = rt.llm

    await rt.reconfigure(settings.replace(free_mode=True, locale="fa", max_history=12))

    assert rt.settings.free_mode is True
    assert rt.store._s.max_history == 12
    assert rt.router._s.free_mode is True
    assert rt.budget._s.free_mode is True
    assert rt.strings.locale == "fa"
    assert rt.router._llm is rt.llm, "the router must not keep the retired client"

    # The old client is retired in the background now, so that a reply already in
    # flight is not cut off by somebody pressing a panel button.
    await asyncio.sleep(0)
    assert first.closed, "the old client's connections are released"
    assert rt.llm.catalog_loads == 1, "the new key needs its own model list"


async def test_reconfiguring_keeps_the_conversations(settings, monkeypatch):
    """A key swap must not make every group forget what it was talking about."""
    monkeypatch.setattr(runtime_mod, "LLMClient", _Client)
    rt = Runtime.build(settings)
    state = rt.store.get(-100)
    state.add_user("reza", "hello")
    store, database = rt.store, rt.db

    await rt.reconfigure(settings.replace(free_mode=True))

    assert rt.store is store
    assert rt.db is database
    assert len(rt.store.get(-100).history) == 1
