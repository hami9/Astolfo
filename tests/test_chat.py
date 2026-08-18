import asyncio
import base64
import io

from PIL import Image

from astolfo import chat
from astolfo.llm import Citation, Usage
from astolfo.persona import FAST, SEARCH, THINK
from astolfo.routing import Decision
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update


async def run(rt, message: FakeMessage, bot: FakeBot | None = None) -> FakeContext:
    context = FakeContext(rt, bot or FakeBot())
    await chat.handle_message(make_update(message), context)
    return context


# -- participation -------------------------------------------------------
async def test_direct_mention_always_answers(rt, llm):
    rt.settings = rt.settings.replace(group_reply_chance=0.0)
    message = FakeMessage("astolfo what do you think?")
    await run(rt, message)

    assert message.sent
    assert len(llm.calls) == 1
    state = rt.store.get(message.chat.id)
    assert len(state.history) == 2
    assert state.history[0]["content"].startswith("Reza:")


async def test_silent_when_not_addressed(rt, llm):
    rt.settings = rt.settings.replace(group_reply_chance=0.0)
    message = FakeMessage("just people talking")
    await run(rt, message)

    assert not message.sent
    assert not llm.calls
    assert len(rt.store.get(message.chat.id).history) == 1, "message is still remembered"


async def test_private_chat_always_answers(rt):
    rt.settings = rt.settings.replace(group_reply_chance=0.0)
    message = FakeMessage("hi", chat_id=555, chat_type="private")
    await run(rt, message)
    assert message.sent


async def test_other_bots_are_ignored(rt, llm):
    rt.settings = rt.settings.replace(group_reply_chance=1.0)
    message = FakeMessage("astolfo hello", is_bot=True)
    await run(rt, message)
    assert not message.sent and not llm.calls


async def test_muted_chat_stays_silent(rt):
    rt.store.get(-100).muted = True
    message = FakeMessage("astolfo answer me")
    await run(rt, message)
    assert not message.sent


async def test_no_concurrent_replies_per_chat(rt, llm):
    async def slow(messages, **kwargs):
        await asyncio.sleep(0.15)
        return await FakeLLMChat(llm)(messages, **kwargs)

    llm.chat = slow
    first, second = FakeMessage("astolfo one"), FakeMessage("astolfo two")
    await asyncio.gather(run(rt, first), run(rt, second))

    assert len(llm.calls) == 1
    assert bool(first.sent) != bool(second.sent)


class FakeLLMChat:
    def __init__(self, llm):
        self.llm = llm

    async def __call__(self, messages, **kwargs):
        from astolfo.llm import ChatResult

        self.llm.calls.append({"messages": messages, **kwargs})
        return ChatResult(text=self.llm.reply, model="fake", usage=self.llm.usage)


# -- output shaping ------------------------------------------------------
async def test_reply_is_polished(rt, llm):
    llm.reply = "Astolfo: **hey** friend!"
    message = FakeMessage("astolfo hi")
    await run(rt, message)
    assert message.sent == ["hey friend!"]


async def test_long_reply_is_split(rt, llm):
    llm.reply = "a" * 9000
    message = FakeMessage("astolfo say something long")
    await run(rt, message)
    assert len(message.sent) >= 3
    assert all(len(chunk) <= 3900 for chunk in message.sent)


async def test_sources_only_in_search_mode(rt, llm):
    llm.citations = [Citation("News", "https://example.com/x")]

    search_msg = FakeMessage("astolfo what's the dollar price today?")
    await run(rt, search_msg)
    assert "https://example.com/x" in search_msg.sent[0]
    assert llm.calls[0]["web"] is True

    chat_msg = FakeMessage("astolfo hi", chat_id=-200)
    await run(rt, chat_msg)
    assert "example.com" not in chat_msg.sent[0]
    assert llm.calls[1]["web"] is False


async def test_model_failure_returns_in_character_line(rt, llm):
    llm.reply = None
    message = FakeMessage("astolfo hi")
    await run(rt, message)
    assert message.sent and message.sent[0] == rt.strings("error_reply")


# -- prompt assembly -----------------------------------------------------
async def test_prompt_layout(rt, llm):
    state = rt.store.get(-100)
    state.notes = "Reza loves coffee"
    message = FakeMessage("astolfo hey")
    await run(rt, message)

    messages = llm.calls[0]["messages"]
    assert messages[0]["role"] == "system" and "<identity>" in messages[0]["content"]
    assert messages[1]["role"] == "system" and "<response-mode" in messages[1]["content"]
    assert "Reza loves coffee" in messages[1]["content"]
    assert messages[-1]["role"] == "user" and "Reza: astolfo hey" in messages[-1]["content"]


async def test_static_block_is_identical_across_turns(rt, llm):
    for text in ("astolfo hi", "astolfo again", "astolfo one more"):
        await run(rt, FakeMessage(text))
    blocks = {call["messages"][0]["content"] for call in llm.calls}
    assert len(blocks) == 1, "cacheable prefix must not change between turns"


async def test_persona_reminder_is_reinjected(rt, llm):
    rt.settings = rt.settings.replace(persona_reinject_every=2, group_reply_chance=0.0)
    state = rt.store.get(-100)
    state.add_user("Reza", "filler")  # turn_count 1, next user turn makes it 2

    await run(rt, FakeMessage("astolfo hey"))
    from astolfo.persona import REMINDER

    assert any(m.get("content") == REMINDER for m in llm.calls[0]["messages"])


async def test_history_is_trimmed_by_char_budget(rt, llm):
    rt.settings = rt.settings.replace(history_char_budget=200)
    state = rt.store.get(-100)
    for i in range(10):
        state.add_user("Reza", f"{i} " + "x" * 150)

    await run(rt, FakeMessage("astolfo hey"))
    history = [m for m in llm.calls[0]["messages"] if m["role"] in ("user", "assistant")]
    assert len(history) <= 3


# -- routing and model selection ----------------------------------------
def test_model_params_per_mode(settings):
    fast = chat.model_params(settings, Decision(FAST), has_media=False)
    assert fast["model"] == settings.model_fast
    assert fast["reasoning"] == {"max_tokens": 0}

    think = chat.model_params(settings, Decision(THINK), has_media=False)
    assert think["model"] == settings.model_think
    assert think["reasoning"]["effort"] == settings.think_effort
    assert think["max_tokens"] > fast["max_tokens"]

    search = chat.model_params(settings, Decision(SEARCH, web=True), has_media=False)
    assert search["temperature"] <= 0.3

    media = chat.model_params(settings, Decision(FAST), has_media=True)
    assert media["model"] == settings.model_media


async def test_forced_mode_from_chat_settings(rt, llm):
    rt.store.get(-100).forced_mode = THINK
    await run(rt, FakeMessage("astolfo hi"))
    assert llm.calls[0]["model"] == rt.settings.model_think


# -- cost controls -------------------------------------------------------
async def test_response_cache_avoids_second_call(rt, llm):
    rt.settings = rt.settings.replace(response_cache=True)
    first = FakeMessage("astolfo tell me a joke")
    second = FakeMessage("astolfo   TELL me a joke  ")

    await run(rt, first)
    await run(rt, second)

    assert len(llm.calls) == 1, "identical question must be served from cache"
    assert second.sent == first.sent
    assert rt.budget.summary()["cache_replies"] == 1


async def test_response_cache_skips_media_and_search(rt, llm):
    rt.settings = rt.settings.replace(response_cache=True)
    for _ in range(2):
        await run(rt, FakeMessage("astolfo what is the dollar price today?"))
    assert len(llm.calls) == 2, "search answers must not be cached"


async def test_budget_blocks_unaddressed_messages(rt, llm):
    rt.settings = rt.settings.replace(daily_budget_usd=0.01, group_reply_chance=1.0)
    rt.budget._s = rt.settings
    rt.budget.record(mode="fast", model="m", usage=Usage(cost=0.011), chat_id=-100)

    message = FakeMessage("random chatter in the group")
    await run(rt, message)
    assert not llm.calls
    assert not message.sent


async def test_budget_degrades_to_fast_before_stopping(rt, llm):
    rt.settings = rt.settings.replace(daily_budget_usd=1.0)
    rt.budget._s = rt.settings
    rt.budget.record(mode="think", model="m", usage=Usage(cost=0.85), chat_id=-100)

    await run(rt, FakeMessage("astolfo why does this python error happen?"))
    assert llm.calls[0]["model"] == rt.settings.model_fast
    assert llm.calls[0]["web"] is False


async def test_budget_exhausted_notifies_once(rt, llm):
    rt.settings = rt.settings.replace(daily_budget_usd=0.01)
    rt.budget._s = rt.settings
    rt.budget.record(mode="fast", model="m", usage=Usage(cost=1.0), chat_id=-100)

    first, second = FakeMessage("astolfo hi"), FakeMessage("astolfo hello")
    await run(rt, first)
    await run(rt, second)

    assert first.sent == [rt.strings("budget_stopped")]
    assert second.sent == [], "the notice is throttled to once an hour"
    assert not llm.calls


async def test_usage_is_recorded_per_call(rt, llm):
    await run(rt, FakeMessage("astolfo hi"))
    summary = rt.budget.summary()
    assert summary["calls"] == 1
    assert summary["cost_today"] == round(llm.usage.cost, 4)


# -- media ---------------------------------------------------------------
def _png(size=(2000, 1400)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 40, 90)).save(buf, format="PNG")
    return buf.getvalue()


async def test_photo_is_downscaled_and_routed_to_vision_model(rt, llm):
    from types import SimpleNamespace

    raw = _png()
    message = FakeMessage(None, caption="astolfo look at this")
    message.photo = [SimpleNamespace(file_id="f1", file_size=len(raw), width=2000, height=1400)]

    await run(rt, message, FakeBot(file_bytes=raw))

    assert message.sent
    assert llm.calls[0]["model"] == rt.settings.model_media

    content = llm.calls[0]["messages"][-1]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert "astolfo look at this" in content[0]["text"]

    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as img:
        assert max(img.size) <= rt.settings.image_max_dim

    assert "[sent a photo]" in rt.store.get(message.chat.id).history[0]["content"]


async def test_oversized_media_is_reported_not_downloaded(rt, llm):
    from types import SimpleNamespace

    message = FakeMessage(None, caption="astolfo watch")
    message.video = SimpleNamespace(
        file_id="v1", file_size=100 * 1024 * 1024, duration=10, thumbnail=None
    )
    await run(rt, message)

    text = llm.calls[0]["messages"][-1]["content"]
    assert isinstance(text, str)
    assert "too large" in text


async def test_backlog_reaches_the_model_as_one_block(rt, llm):
    """The pile-up of unanswered group messages must not arrive as many turns."""
    rt.settings = rt.settings.replace(group_reply_chance=0.0)
    state = rt.store.get(-100)
    for text in ("what are these?", "edit it?", "chance 25?", "it was 100"):
        state.add_user("Hami", text)

    await run(rt, FakeMessage("astolfo so what do you think"))

    body = llm.calls[0]["messages"]
    conversation = [m for m in body if m["role"] in ("user", "assistant")]
    assert len(conversation) == 2, "one merged backlog turn plus the current message"
    assert conversation[0]["content"].count("\n") == 3
    assert "so what do you think" in conversation[-1]["content"]


async def test_failure_apology_is_throttled(rt, llm):
    """A provider outage must not turn every message into an apology."""
    llm.reply = None
    messages = [FakeMessage("astolfo hi"), FakeMessage("astolfo hello"),
                FakeMessage("astolfo anyone there")]
    for message in messages:
        await run(rt, message)

    apologies = [m for m in messages if m.sent]
    assert len(apologies) == 1, "only the first failure in the window is announced"
    assert apologies[0].sent == [rt.strings("error_reply")]
    assert len(llm.calls) == 3, "the bot still tries every time"


async def test_apology_returns_after_the_window(rt, llm):
    llm.reply = None
    first = FakeMessage("astolfo hi")
    await run(rt, first)

    rt.store.get(-100).error_notice_at -= chat.ERROR_NOTICE_INTERVAL + 1
    second = FakeMessage("astolfo still broken?")
    await run(rt, second)

    assert first.sent and second.sent


async def test_out_of_credit_says_so_instead_of_the_generic_apology(rt, llm):
    from astolfo.llm import ChatResult

    async def broke(messages, **kwargs):
        llm.calls.append({"messages": messages, **kwargs})
        return ChatResult(error="HTTP 402: out of credit", error_kind="payment")

    llm.chat = broke
    message = FakeMessage("astolfo hi")
    await run(rt, message)

    assert message.sent == [rt.strings("no_credit")]
    assert rt.strings("no_credit") != rt.strings("error_reply")


async def test_out_of_credit_notice_is_rare(rt, llm):
    from astolfo.llm import ChatResult

    async def broke(messages, **kwargs):
        return ChatResult(error="HTTP 402", error_kind="payment")

    llm.chat = broke
    first, second = FakeMessage("astolfo hi"), FakeMessage("astolfo hello")
    await run(rt, first)
    await run(rt, second)

    assert first.sent and not second.sent, "credit runs out once, not every message"
