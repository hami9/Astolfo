"""Five bugs a run against the live server turned up.

Each test here is written from something that actually happened in the log, not
from something that might: a crash on every panel press, a dead callback query
eating the settings change behind it, images going to models that cannot see,
one free model answering with silence twenty times, and twenty-one replies into
a group the bot is not allowed to post in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from astolfo import runtime as runtime_mod
from astolfo.chat import send_reply
from astolfo.llm import EMPTY_STRIKES, LLMClient
from astolfo.runtime import Runtime
from tests.conftest import FakeMessage


def _settings(settings, **overrides):
    return settings.replace(providers=["openrouter"], free_mode=True, **overrides)


# -- 1. closing a client under a request in flight -------------------------
async def test_a_request_in_flight_survives_the_client_being_retired(settings, monkeypatch):
    """The crash was RuntimeError: Cannot send a request, as the client has been
    closed - raised inside whatever chat was mid-reply when a panel button moved."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        started.set()
        await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(_settings(settings), transport=httpx.MockTransport(slow))

    turn = asyncio.create_task(client.chat([{"role": "user", "content": "hi"}], model="m"))
    await started.wait()
    closing = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)

    assert not closing.done(), "the close waits rather than pulling the pool out"
    release.set()
    assert (await turn).text == "hi", "the reply still arrives"
    # wait_for rather than a bare `await closing`: it asserts the close actually
    # completes once the request lets go, and reads as doing something, which a
    # bare await on a task name does not - to CodeQL or to the next reader.
    await asyncio.wait_for(closing, timeout=5.0)


async def test_the_drain_gives_up_rather_than_hanging_forever(settings, monkeypatch):
    """A wedged request must not hold the connection pools for the whole process."""
    monkeypatch.setattr("astolfo.llm.DRAIN_TIMEOUT", 0.01)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    async def never(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = LLMClient(_settings(settings), transport=httpx.MockTransport(never))
    stuck = asyncio.create_task(client.chat([{"role": "user", "content": "x"}], model="m"))
    await asyncio.sleep(0.01)

    await asyncio.wait_for(client.aclose(), timeout=2.0)
    stuck.cancel()


async def test_retiring_a_client_does_not_block_the_panel(settings, monkeypatch):
    """Pressing a button must return now, not when the slowest reply finishes."""

    class _Client:
        def __init__(self, settings, registry=None):
            self.closed = False
            self.providers = [SimpleNamespace(name="openrouter")]

        async def load_catalog(self):
            return None

        async def aclose(self):
            await asyncio.sleep(0.05)
            self.closed = True

    monkeypatch.setattr(runtime_mod, "LLMClient", _Client)
    rt = Runtime.build(settings)
    first = rt.llm

    await rt.reconfigure(settings.replace(locale="fa"))
    assert not first.closed, "reconfigure returned without waiting on the drain"

    await asyncio.sleep(0.1)
    assert first.closed, "and the old pools are still closed once it finishes"


# -- 2. a dead callback query --------------------------------------------
async def test_an_expired_query_does_not_swallow_the_settings_change(settings, monkeypatch):
    """answer() used to raise on the line before the reload, losing both."""
    from astolfo.admin import panel

    reloaded, edited = [], []

    class _Query:
        data = "ap:home"
        message = None
        from_user = SimpleNamespace(id=1)

        async def answer(self, *args, **kwargs):
            raise RuntimeError("Query is too old and response timeout expired")

    monkeypatch.setattr(panel, "allowed", lambda *a: True)
    monkeypatch.setattr(panel, "_edit", lambda *a: edited.append(True) or _done())
    monkeypatch.setattr(panel.settings_store, "reload", lambda db: settings)

    async def _route(ctx, parts):
        view = panel.View("ok")
        view.extras["reload"] = True
        return view

    monkeypatch.setattr(panel, "_route", _route)

    rt = SimpleNamespace(
        db=None, reconfigure=lambda s: _record(reloaded), settings=settings,
    )
    monkeypatch.setattr(panel.runtime, "get", lambda ctx: rt)

    update = SimpleNamespace(callback_query=_Query(), effective_user=SimpleNamespace(id=1))
    context = SimpleNamespace(user_data={}, bot=None)

    await panel.on_button(update, context)

    assert reloaded, "the settings change still happened"
    assert edited, "and the view was still redrawn"


async def _done():
    return None


def _record(into):
    into.append(True)
    return _done()


async def test_a_redraw_that_fails_says_so(settings, monkeypatch, caplog):
    """Suppressing this silently was my own regression: a panel that quietly
    stops updating with nothing in the log is worse to chase than one that throws."""
    from astolfo.admin import panel

    class _Query:
        data = "ap:home"
        message = None
        from_user = SimpleNamespace(id=1)

        async def answer(self, *args, **kwargs):
            return None

    async def _boom(*args):
        raise RuntimeError("Message is too long")

    monkeypatch.setattr(panel, "allowed", lambda *a: True)
    monkeypatch.setattr(panel, "_edit", _boom)
    monkeypatch.setattr(panel.runtime, "get", lambda ctx: SimpleNamespace(db=None))

    async def _route(ctx, parts):
        return panel.View("ok")

    monkeypatch.setattr(panel, "_route", _route)

    update = SimpleNamespace(callback_query=_Query(), effective_user=SimpleNamespace(id=1))
    with caplog.at_level("WARNING"):
        await panel.on_button(update, SimpleNamespace(user_data={}, bot=None))

    assert "could not redraw the panel" in caplog.text
    assert "Message is too long" in caplog.text, "and says what actually went wrong"


# -- 3. images to models that cannot see ----------------------------------
async def test_one_model_refusing_an_image_does_not_blind_its_whole_service(
    settings, monkeypatch
):
    """One refusal from a free model used to mark the whole of OpenRouter
    text-only, which took its real vision models down with it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(_settings(settings), transport=httpx.MockTransport(_ok))
    client._free_vision = ["vendor/blind", "vendor/sighted"]
    client._free_text = ["vendor/blind", "vendor/sighted"]
    client._text_only.add(("openrouter", "vendor/blind"))

    assert client.blind_to("openrouter", "vendor/blind")
    assert not client.blind_to("openrouter", "vendor/sighted")
    assert not client.blind_to("google", "vendor/blind"), "a fact about one service"

    provider = client.providers[0]
    assert client.can_see(provider), "its other vision model is still usable"
    assert "vendor/blind" not in client._candidates(provider, "x", vision=True)


async def test_a_service_whose_every_model_refused_is_skipped(settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(_settings(settings), transport=httpx.MockTransport(_ok))
    client._free_vision = ["vendor/blind"]
    client._free_text = ["vendor/blind"]
    client._text_only.add(("openrouter", "vendor/blind"))

    assert not client.can_see(client.providers[0])


def _ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": []})
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


# -- 4. a model that answers with silence ---------------------------------
def test_a_repeat_offender_earns_a_longer_rest_each_time(settings, monkeypatch):
    """It came back every ten minutes and wasted a turn, twenty times in one log."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(_settings(settings), transport=httpx.MockTransport(_ok))

    rests = []
    monkeypatch.setattr(client, "_rest", lambda model, seconds: rests.append(seconds))
    for _ in range(4):
        client.mark_unusable("minimax/minimax-m3:free")

    assert rests[0] < rests[1] < rests[2], "each offence costs it more"
    assert rests[2] == EMPTY_STRIKES[-1] and rests[3] == EMPTY_STRIKES[-1], "and then caps"


def test_a_caller_may_still_name_its_own_cooldown(settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(_settings(settings), transport=httpx.MockTransport(_ok))
    rests = []
    monkeypatch.setattr(client, "_rest", lambda model, seconds: rests.append(seconds))

    client.mark_unusable("x", seconds=5.0)
    assert rests == [5.0]


def test_strikes_are_counted_per_model(settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(_settings(settings), transport=httpx.MockTransport(_ok))
    rests = []
    monkeypatch.setattr(client, "_rest", lambda model, seconds: rests.append(seconds))

    client.mark_unusable("a")
    client.mark_unusable("a")
    client.mark_unusable("b")

    assert rests[2] == EMPTY_STRIKES[0], "b is on its first strike, not a's second"


# -- 5. a group that will not let it post ---------------------------------
class _Refusing(FakeMessage):
    async def reply_text(self, text, **kwargs):
        raise RuntimeError(
            "Telegram says: Bad Request: not enough rights to send text messages to the chat"
        )


async def test_a_group_that_refuses_the_bot_is_switched_off(rt):
    message = _Refusing("hello")
    await send_reply(message, "hi there", rt)

    assert message.chat.id in rt.dormant
    assert rt.store.get(message.chat.id).off
    assert any(row["action"] == "chat_muted_no_rights" for row in rt.db.audit_trail())


async def test_an_ordinary_send_failure_leaves_the_chat_alone(rt):
    class _Flaky(FakeMessage):
        async def reply_text(self, text, **kwargs):
            raise RuntimeError("Timed out")

    message = _Flaky("hello")
    await send_reply(message, "hi there", rt)

    assert message.chat.id not in rt.dormant, "a network blip is not a permission problem"


async def test_it_still_sends_when_nothing_is_wrong(rt):
    message = FakeMessage("hello")
    await send_reply(message, "hi there", rt)

    assert message.sent == ["hi there"]
    assert message.chat.id not in rt.dormant


# -- 6. a bad model must not come back first after a restart --------------
def _client(settings, registry=None):
    return LLMClient(_settings(settings), transport=httpx.MockTransport(_ok), registry=registry)


def test_a_models_record_survives_a_restart(settings, monkeypatch):
    """The startup log showed the model that answered with silence twenty times
    picked first for five of six jobs: strikes lived only in memory, and they
    restart the bot every time they update."""
    from astolfo.crypto import SecretBox
    from astolfo.db import open_database
    from astolfo.services import ServiceRegistry

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    database = open_database(settings.data_dir)
    registry = ServiceRegistry(database, SecretBox(settings.data_dir))

    before = _client(settings, registry)
    for _ in range(3):
        before.mark_unusable("minimax/minimax-m3:free")

    after = _client(settings, registry)
    assert after._strikes["minimax/minimax-m3:free"] == 3
    assert after._scores["minimax/minimax-m3:free"] < 0, "it starts behind, not level"


def test_the_worst_model_sinks_to_the_back_of_the_pool(settings, monkeypatch):
    """Sunk, not banned: the free pool is ordered widest-context first, and the
    widest model is not always a working one. Three strikes buy it a long rest,
    which now outlives the restart too, so the pool is checked either side of it."""
    from astolfo.crypto import SecretBox
    from astolfo.db import open_database
    from astolfo.services import ServiceRegistry

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    registry = ServiceRegistry(open_database(settings.data_dir), SecretBox(settings.data_dir))

    first = _client(settings, registry)
    first._free_text = ["wide/but-broken", "narrow/but-fine"]
    for _ in range(3):
        first.mark_unusable("wide/but-broken")

    after = _client(settings, registry)
    after._free_text = ["wide/but-broken", "narrow/but-fine"]

    assert after.free_pool() == ["narrow/but-fine"], "the broken one is still resting"

    after._cooldowns.clear()
    pool = after.free_pool()

    assert pool[0] == "narrow/but-fine", "the working one is asked first now"
    assert "wide/but-broken" in pool, "and the other is still tried, just last"


def test_a_record_cannot_sink_a_model_without_limit(settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = _client(settings)
    for _ in range(20):
        client.mark_unusable("x")

    assert client._scores["x"] == -guard_sink(), "capped, so six good replies can undo it"


def guard_sink() -> int:
    from astolfo.llm import MAX_SINK

    return MAX_SINK


def test_a_model_forgiven_by_pruning_starts_level_again(settings):
    """Weights, hardware and endpoints all change under the same id."""
    import time as _time

    from astolfo.db import open_database

    database = open_database(settings.data_dir)
    database.note_strike("was/bad-once")
    database.execute("UPDATE model_health SET last_bad = ?", (_time.time() - 200 * 86400,))

    assert database.prune(90)["model_health"] == 1
    assert database.model_strikes() == {}


def test_recording_a_strike_never_breaks_a_turn(settings, monkeypatch):
    """The database is a convenience here; answering the chat is not."""

    class _Broken:
        def rows(self):
            return None

        def model_strikes(self):
            raise RuntimeError("disk is full")

        def note_strike(self, model):
            raise RuntimeError("disk is full")

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = _client(settings, _Broken())
    client.mark_unusable("x")

    assert client._strikes["x"] == 1, "it still counts in memory"
