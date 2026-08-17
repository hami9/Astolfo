"""تست جریان کامل پاسخ‌دهی با اشیای جعلی — بدون شبکه و بدون تلگرام واقعی.

هدف: مطمئن شویم مسیر اصلی handle_message بدون خطا کار می‌کند، تاریخچه درست ثبت
می‌شود، خروجی پاک‌سازی می‌شود و منطق ورود خودکار به بحث گروه رعایت می‌گردد.

اجرا: python -m tests.test_flow
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

from astolfo import handlers, router  # noqa: E402
from astolfo.ai import ChatResult, Citation  # noqa: E402
from astolfo.config import Settings  # noqa: E402
from astolfo.memory import ChatStore  # noqa: E402


# ---------------------------------------------------------------------------
# بدل‌ها
# ---------------------------------------------------------------------------
class FakeAI:
    def __init__(self, reply: str = "یاهو~ چه خبرا؟", citations=None):
        self.reply = reply
        self.citations = citations or []
        self.calls: List[dict] = []
        self.total_tokens = 0

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return ChatResult(text=self.reply, model="fake", citations=self.citations)

    async def json_call(self, messages, **kwargs):
        return None  # مسیریاب مدل غیرفعال؛ فقط قواعد سریع


class FakeBot:
    def __init__(self):
        self.bot = SimpleNamespace(id=999, username="astolfo_bot", first_name="آستولفو")
        self.actions = 0

    async def send_chat_action(self, **kwargs):
        self.actions += 1


class FakeMessage:
    def __init__(self, text=None, *, chat_id=-100, chat_type="supergroup",
                 user_id=1, name="رضا", is_bot=False, reply_to=None, caption=None):
        self.message_id = 1
        self.text = text
        self.caption = caption
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title="گروه تست")
        self.chat_id = chat_id
        self.from_user = SimpleNamespace(
            id=user_id, is_bot=is_bot, first_name=name, username=None
        )
        self.reply_to_message = reply_to
        self.entities = []
        self.caption_entities = []
        self.photo = self.sticker = self.animation = None
        self.video = self.video_note = self.voice = self.audio = self.document = None
        self.sent: List[str] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=2)


def make_context(store: ChatStore, ai: FakeAI, settings: Settings, bot: FakeBot):
    application = SimpleNamespace(
        bot_data={"settings": settings, "store": store, "ai": ai},
        create_task=lambda coro: asyncio.ensure_future(coro),
    )
    return SimpleNamespace(bot=bot, application=application, args=[])


def base_settings(tmp: str, **overrides) -> Settings:
    s = Settings.from_env()
    fields = {
        **s.__dict__,
        "data_dir": tmp,
        "router_llm_enabled": False,
        "summary_enabled": False,
        "reply_cooldown_sec": 0.0,
        **overrides,
    }
    return Settings(**fields)


async def run_message(msg: FakeMessage, settings: Settings, ai: FakeAI, store: ChatStore):
    bot = FakeBot()
    ctx = make_context(store, ai, settings, bot)
    update = SimpleNamespace(effective_message=msg, effective_chat=msg.chat,
                             effective_user=msg.from_user)
    await handlers.handle_message(update, ctx)
    return ctx


# ---------------------------------------------------------------------------
# تست‌ها
# ---------------------------------------------------------------------------
def test_direct_mention_always_answers():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp, group_reply_chance=0.0)
            ai, store = FakeAI(), ChatStore(base_settings(tmp))
            msg = FakeMessage("آستولفو نظرت چیه؟")
            await run_message(msg, settings, ai, store)
            assert msg.sent, "منشن مستقیم باید همیشه جواب بگیرد"
            assert len(ai.calls) == 1
            state = store.get(msg.chat.id)
            assert len(state.history) == 2, "پیام کاربر و پاسخ باید ثبت شوند"
            assert state.history[0]["content"].startswith("رضا:")
            assert state.history[1]["role"] == "assistant"

    asyncio.run(go())


def test_silent_when_not_addressed_and_chance_zero():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp, group_reply_chance=0.0)
            ai, store = FakeAI(), ChatStore(settings)
            msg = FakeMessage("یه حرف معمولی بین بقیه")
            await run_message(msg, settings, ai, store)
            assert not msg.sent, "بدون خطاب و با احتمال صفر نباید جواب بدهد"
            assert not ai.calls, "نباید مدل را صدا بزند"
            state = store.get(msg.chat.id)
            assert len(state.history) == 1, "پیام باید فقط در تاریخچه ثبت شود"

    asyncio.run(go())


def test_private_chat_always_answers():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp, group_reply_chance=0.0)
            ai, store = FakeAI(), ChatStore(settings)
            msg = FakeMessage("سلام", chat_id=555, chat_type="private")
            await run_message(msg, settings, ai, store)
            assert msg.sent, "در پی‌وی همیشه باید جواب بدهد"

    asyncio.run(go())


def test_bot_messages_ignored():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp, group_reply_chance=1.0)
            ai, store = FakeAI(), ChatStore(settings)
            msg = FakeMessage("آستولفو سلام", is_bot=True)
            await run_message(msg, settings, ai, store)
            assert not msg.sent and not ai.calls, "به ربات‌های دیگر جواب ندهد"

    asyncio.run(go())


def test_muted_chat_is_silent():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)
            ai, store = FakeAI(), ChatStore(settings)
            store.get(-100).muted = True
            msg = FakeMessage("آستولفو جواب بده")
            await run_message(msg, settings, ai, store)
            assert not msg.sent

    asyncio.run(go())


def test_reply_is_polished_and_prefix_stripped():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)
            ai = FakeAI(reply="آستولفو: **سلام** رفیق!")
            store = ChatStore(settings)
            msg = FakeMessage("آستولفو سلام")
            await run_message(msg, settings, ai, store)
            assert msg.sent == ["سلام رفیق!"]

    asyncio.run(go())


def test_sources_appended_only_in_search_mode():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)
            cites = [Citation("خبر", "https://example.com/x")]
            store = ChatStore(settings)

            ai = FakeAI(reply="دلار امروز فلان قدره", citations=cites)
            msg = FakeMessage("آستولفو قیمت دلار امروز چنده؟")
            await run_message(msg, settings, ai, store)
            assert "https://example.com/x" in msg.sent[0], "در حالت سرچ باید منبع بیاید"
            assert ai.calls[0]["web"] is True

            ai2 = FakeAI(reply="هه‌هه~", citations=cites)
            msg2 = FakeMessage("آستولفو سلام", chat_id=-200)
            await run_message(msg2, settings, ai2, store)
            assert "example.com" not in msg2.sent[0], "در گپ عادی منبع اضافه نشود"
            assert ai2.calls[0]["web"] is False

    asyncio.run(go())


def test_long_reply_is_split():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)
            ai = FakeAI(reply="آ" * 9000)
            store = ChatStore(settings)
            msg = FakeMessage("آستولفو یه چیز بلند بگو")
            await run_message(msg, settings, ai, store)
            assert len(msg.sent) >= 3
            assert all(len(chunk) <= 3900 for chunk in msg.sent)

    asyncio.run(go())


def test_forced_mode_uses_think_model():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)
            ai, store = FakeAI(), ChatStore(settings)
            store.get(-100).forced_mode = router.THINK
            msg = FakeMessage("آستولفو سلام")
            await run_message(msg, settings, ai, store)
            assert ai.calls[0]["model"] == settings.model_think
            assert ai.calls[0]["reasoning"]["effort"] == settings.think_reasoning_effort

    asyncio.run(go())


def test_empty_model_reply_triggers_fallback_line():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)

            class DeadAI(FakeAI):
                async def chat(self, messages, **kwargs):
                    return ChatResult(text=None, error="boom")

            ai, store = DeadAI(), ChatStore(settings)
            msg = FakeMessage("آستولفو سلام")
            await run_message(msg, settings, ai, store)
            assert msg.sent and "مغزم" in msg.sent[0], "باید پیام خطای درون‌شخصیتی بدهد"

    asyncio.run(go())


def test_no_duplicate_reply_while_busy():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)
            store = ChatStore(settings)

            class SlowAI(FakeAI):
                async def chat(self, messages, **kwargs):
                    await asyncio.sleep(0.15)
                    return await super().chat(messages, **kwargs)

            ai = SlowAI()
            m1, m2 = FakeMessage("آستولفو یک"), FakeMessage("آستولفو دو")
            await asyncio.gather(
                run_message(m1, settings, ai, store),
                run_message(m2, settings, ai, store),
            )
            assert len(ai.calls) == 1, "همزمان دو جواب برای یک چت تولید نشود"
            assert bool(m1.sent) != bool(m2.sent)

    asyncio.run(go())


def test_photo_is_encoded_and_sent_to_vision_model():
    """عکس باید به data-url تبدیل و در قالب چندوجهی به مدل رسانه فرستاده شود."""

    async def go():
        import io

        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp)
            ai, store = FakeAI(reply="اوهو چه گربه‌ای!"), ChatStore(settings)

            buf = io.BytesIO()
            Image.new("RGB", (2000, 1400), (200, 40, 90)).save(buf, format="PNG")
            png_bytes = buf.getvalue()

            class FakeFile:
                async def download_to_drive(self, custom_path=None, **kwargs):
                    with open(custom_path, "wb") as fh:
                        fh.write(png_bytes)

            class PhotoBot(FakeBot):
                async def get_file(self, file_id):
                    return FakeFile()

            msg = FakeMessage(None, caption="آستولفو اینو ببین")
            msg.photo = [SimpleNamespace(file_id="f1", file_size=len(png_bytes),
                                         width=2000, height=1400)]

            bot = PhotoBot()
            ctx = make_context(store, ai, settings, bot)
            update = SimpleNamespace(effective_message=msg, effective_chat=msg.chat,
                                     effective_user=msg.from_user)
            await handlers.handle_message(update, ctx)

            assert msg.sent, "به عکس باید واکنش نشان دهد"
            assert ai.calls[0]["model"] == settings.model_media, "عکس باید به مدل بینایی برود"

            content = ai.calls[0]["messages"][-1]["content"]
            assert isinstance(content, list) and len(content) == 2
            assert content[0]["type"] == "text" and "آستولفو اینو ببین" in content[0]["text"]
            url = content[1]["image_url"]["url"]
            assert url.startswith("data:image/jpeg;base64,"), "تصویر باید JPEG فشرده شود"

            # کوچک‌سازی واقعاً انجام شده باشد
            import base64

            raw = base64.b64decode(url.split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as img:
                assert max(img.size) <= settings.image_max_dim

            state = store.get(msg.chat.id)
            assert "[یک عکس فرستاد]" in state.history[0]["content"]

    asyncio.run(go())


def test_reply_to_bot_counts_as_addressed():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            settings = base_settings(tmp, group_reply_chance=0.0)
            ai, store = FakeAI(), ChatStore(settings)
            bot_msg = SimpleNamespace(from_user=SimpleNamespace(id=999))
            msg = FakeMessage("دقیقاً همینه", reply_to=bot_msg)
            await run_message(msg, settings, ai, store)
            assert msg.sent, "ریپلای روی پیام ربات یعنی خطاب مستقیم"

    asyncio.run(go())


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"❌ {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"💥 {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} تست پاس شد.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
