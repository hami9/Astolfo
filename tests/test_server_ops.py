"""Asking the server for the two things the bot cannot do itself."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from astolfo import server_ops
from astolfo.admin import server as server_section
from tests.conftest import FakeBot


def _ctx(rt):
    return SimpleNamespace(rt=rt, user=SimpleNamespace(id=4242), bot=FakeBot())


def test_only_the_two_known_jobs_are_accepted(settings):
    """Whatever arrives from a chat, the helper's vocabulary stays this short."""
    for action in ("rm -rf /", "restart; reboot", "shutdown", ""):
        ok, detail = server_ops.request(settings.data_dir, action)
        assert not ok
        assert "not something" in detail
    assert not os.path.exists(
        os.path.join(settings.data_dir, server_ops.CONTROL_DIR, server_ops.REQUEST_FILE)
    )


@pytest.mark.parametrize("action", server_ops.ACTIONS)
def test_a_job_is_left_as_one_word(settings, action):
    ok, _ = server_ops.request(settings.data_dir, action)
    assert ok

    path = os.path.join(settings.data_dir, server_ops.CONTROL_DIR, server_ops.REQUEST_FILE)
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == action


def test_the_result_of_the_last_job_is_read_back(settings):
    server_ops.request(settings.data_dir, "restart")
    path = os.path.join(settings.data_dir, server_ops.CONTROL_DIR, server_ops.RESULT_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("restarted, running abc123 something\n")

    assert "abc123" in server_ops.last_result(settings.data_dir)
    assert server_ops.result_age(settings.data_dir) < 60


def test_health_reads_without_privileges(settings):
    used, total = server_ops.memory()
    assert total >= used >= 0
    assert isinstance(server_ops.uptime(), str)
    assert isinstance(server_ops.load(), str)


# -- the panel screen -----------------------------------------------------
async def test_restarting_takes_a_second_press(settings, monkeypatch):
    from astolfo import runtime as runtime_mod
    from astolfo.runtime import Runtime

    monkeypatch.setattr(runtime_mod, "LLMClient", lambda s: SimpleNamespace(providers=[]))
    rt = Runtime.build(settings)
    ctx = _ctx(rt)

    first = server_section.job(ctx, "restart", confirmed=False)
    assert "Restart the bot?" in first.text
    assert server_ops.last_result(settings.data_dir) == ""

    server_section.job(ctx, "restart", confirmed=True)
    path = os.path.join(settings.data_dir, server_ops.CONTROL_DIR, server_ops.REQUEST_FILE)
    assert os.path.exists(path)
    assert rt.db.audit_trail()[0]["action"] == "restart"


async def test_an_update_leaves_a_note_to_report_back(settings, monkeypatch):
    from astolfo import runtime as runtime_mod
    from astolfo.runtime import Runtime

    monkeypatch.setattr(runtime_mod, "LLMClient", lambda s: SimpleNamespace(providers=[]))
    rt = Runtime.build(settings)

    server_section.job(_ctx(rt), "update", confirmed=True)
    assert rt.db.note("report_to") == "4242", "the bot is about to be replaced mid-action"
