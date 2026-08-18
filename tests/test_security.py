"""Properties that must hold however the rest of the bot changes."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from astolfo import runtime as runtime_mod
from astolfo import settings_store
from astolfo.admin import guard, open_panel
from astolfo.chat import handle_message
from astolfo.runtime import Runtime
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update

MASTER = 4242


@pytest.fixture
def owned(settings, monkeypatch) -> Runtime:
    monkeypatch.setattr(
        runtime_mod, "LLMClient", lambda s: SimpleNamespace(providers=[], resolve=lambda m, **k: m)
    )
    monkeypatch.setenv("MASTER_ID", str(MASTER))
    return Runtime.build(settings.replace(master_id=MASTER))


async def test_a_blocked_person_gets_no_reply_at_all(rt, bot):
    rt.set_blocked(1, True)
    message = FakeMessage("astolfo hello")
    await handle_message(make_update(message), FakeContext(rt, bot))
    assert message.sent == []
    assert rt.llm.calls == [], "not even a model call"

    rt.set_blocked(1, False)
    await handle_message(make_update(FakeMessage("astolfo hello again")), FakeContext(rt, bot))
    assert rt.llm.calls, "unblocking brings them back"


async def test_blocking_survives_a_restart(settings, rt):
    rt.set_blocked(55, True)
    reopened = Runtime.build(settings)
    assert 55 in reopened.blocked


async def test_repeated_attempts_are_recorded_but_not_answered(owned, caplog):
    stranger = FakeMessage("/panel", chat_id=99, chat_type="private", user_id=99)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await open_panel(make_update(stranger), FakeContext(owned, FakeBot()))

    assert stranger.sent == []
    assert [row["action"] for row in owned.db.audit_trail()] == ["panel_refused"]
    assert sum("refused the panel" in r.message for r in caplog.records) <= guard.LOG_AFTER


async def test_a_key_never_reaches_the_log_or_the_audit_trail(owned, caplog):
    with caplog.at_level(logging.DEBUG):
        settings_store.store_secret(
            owned.db, owned.box, "GROQ_API_KEY", "gsk-do-not-print-me", by=MASTER
        )
        settings_store.export_secrets(owned.db, owned.box)

    written = " ".join(r.getMessage() for r in caplog.records)
    trail = " ".join(f"{r['action']} {r['detail']}" for r in owned.db.audit_trail())
    assert "gsk-do-not-print-me" not in written
    assert "gsk-do-not-print-me" not in trail


def test_the_panel_cannot_reach_a_setting_that_would_lock_the_owner_out(owned):
    editable = settings_store.editable()
    for name in ("telegram_token", "master_id", "master_username", "data_dir"):
        assert name not in editable, name


async def test_the_bot_still_answers_a_stranger_normally(rt, bot):
    """Blocking is the exception; the bot is not suspicious of everyone."""
    message = FakeMessage("astolfo hi", user_id=12345)
    await handle_message(make_update(message), FakeContext(rt, bot))
    assert message.sent
