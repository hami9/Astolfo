"""تست‌های آفلاین (بدون نیاز به شبکه، توکن تلگرام یا کلید API).

اجرا:
    python -m tests.test_offline
    # یا
    pytest tests/
"""

from __future__ import annotations

import asyncio
import collections
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

from astolfo import persona, router  # noqa: E402
from astolfo.ai import _loads_loose  # noqa: E402
from astolfo.config import Settings  # noqa: E402
from astolfo.handlers import _build_messages, _mode_params  # noqa: E402
from astolfo.media import PLACEHOLDERS, MediaBundle  # noqa: E402
from astolfo.memory import ChatState, ChatStore  # noqa: E402
from astolfo.utils import format_sources, polish, split_message  # noqa: E402


def _settings(**overrides) -> Settings:
    base = Settings.from_env()
    if overrides:
        return Settings(**{**base.__dict__, **overrides})
    return base


# ---------------------------------------------------------------------------
def test_split_message():
    text = "خط\n" * 4000
    chunks = list(split_message(text, limit=500))
    assert chunks, "پیام نباید خالی برگردد"
    assert all(len(c) <= 500 for c in chunks), "هیچ تکه‌ای نباید از سقف رد شود"
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")

    assert list(split_message("سلام")) == ["سلام"]
    assert list(split_message("   ")) == []


def test_polish():
    assert polish("آستولفو: سلام!") == "سلام!"
    assert polish("**مهم** و __تاکید__") == "مهم و تاکید"
    assert "#" not in polish("### تیتر\nمتن")
    assert polish("- یک\n- دو").startswith("• ")
    assert "هوش مصنوعی" not in polish("به عنوان یک هوش مصنوعی نمی‌تونم")
    # بلوک کد دست‌نخورده می‌ماند
    code = "بیا:\n```py\nx = 1\n```"
    assert polish(code) == code


def test_format_sources():
    class C:
        def __init__(self, title, url):
            self.title, self.url = title, url

    out = format_sources([C("یک", "https://a.example"), C("دو", "https://a.example")])
    assert out.count("https://a.example") == 1, "لینک تکراری حذف شود"
    assert format_sources([]) == ""


# ---------------------------------------------------------------------------
def test_router_heuristics():
    cases = {
        "سلام چطوری": router.FAST,
        "خخخخ": router.FAST,
        "ok": router.FAST,
        "قیمت دلار امروز چنده؟": router.SEARCH,
        "آخرین نسخهٔ پایتون چیه؟": router.SEARCH,
        "سرچ کن ببین کی برنده شد": router.SEARCH,
        "چطور این ارور رو دیباگ کنم؟": router.THINK,
        "فرق بین لیست و تاپل تو پایتون چیه": router.THINK,
        "یه کد پایتون بنویس که فایل بخونه": router.THINK,
        "خیلی داغونم، حالم بده": router.SERIOUS,
        "i feel awful today": router.SERIOUS,
    }
    for text, expected in cases.items():
        decision, confidence = router.heuristic_decision(text)
        assert decision.mode == expected, f"«{text}» → {decision.mode} (انتظار {expected})"
        assert 0.0 <= confidence <= 1.0

    # حالت جست‌وجو همیشه باید وب را روشن کند
    d, _ = router.heuristic_decision("قیمت دلار امروز چنده؟")
    assert d.web is True

    # پیام بدون متن ولی با رسانه
    d, conf = router.heuristic_decision("", has_media=True)
    assert d.mode == router.FAST and conf >= 0.85


def test_router_forced_mode():
    async def go():
        settings = _settings()
        d = await router.decide(None, settings, text="هرچی", forced_mode="think")
        assert d.mode == router.THINK and d.source == "user"
        d = await router.decide(None, settings, text="سلام")
        assert d.mode == router.FAST  # اطمینان بالا → بدون فراخوانی مدل

    asyncio.run(go())


# ---------------------------------------------------------------------------
def test_persona_layers():
    prompt = persona.build_system_prompt(mode="fast", is_group=True)
    for marker in ("<identity>", "<voice>", "<canon-anchors>", "<never>", "<truthfulness"):
        assert marker in prompt, f"لایهٔ {marker} گم شده"
    assert 'mode="group"' in prompt
    assert "<media>" not in prompt

    media_prompt = persona.build_system_prompt(mode="think", is_group=False, has_media=True)
    assert "<media>" in media_prompt
    assert 'mode="private"' in media_prompt
    assert 'name="think"' in media_prompt

    noted = persona.build_system_prompt(mode="search", notes="رضا عاشق قهوه‌ست", participants=["رضا"])
    assert "رضا عاشق قهوه‌ست" in noted and 'name="search"' in noted

    # هر چهار حالت بلوک مخصوص خودش را دارد
    for mode in (router.FAST, router.THINK, router.SEARCH, router.SERIOUS):
        assert f'name="{mode}"' in persona.build_system_prompt(mode=mode)


# ---------------------------------------------------------------------------
def test_mode_params():
    settings = _settings()

    fast = _mode_params(settings, router.Decision(router.FAST), has_media=False)
    assert fast["model"] == settings.model_fast
    assert fast["reasoning"] == {"max_tokens": 0}, "حالت سریع نباید فکر کند"

    think = _mode_params(settings, router.Decision(router.THINK), has_media=False)
    assert think["model"] == settings.model_think
    assert think["reasoning"]["effort"] == settings.think_reasoning_effort
    assert think["max_tokens"] > fast["max_tokens"]

    search = _mode_params(settings, router.Decision(router.SEARCH, web=True), has_media=False)
    assert search["temperature"] <= 0.3, "پاسخ مبتنی بر منبع باید دمای پایین داشته باشد"

    # رسانه در حالت سریع باید به مدل چندوجهی برود
    with_media = _mode_params(settings, router.Decision(router.FAST), has_media=True)
    assert with_media["model"] == settings.model_media


def test_build_messages_shapes():
    settings = _settings()
    state = ChatState(chat_id=1, history=collections.deque(maxlen=10))
    state.add_user("رضا", "سلام")
    state.add_assistant("یاهو~")
    state.add_user("سارا", "این عکسو ببین [یک عکس فرستاد]")

    bundle = MediaBundle(
        parts=[{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}],
        notes=["یک استیکر تلگرام است."],
        placeholder=PLACEHOLDERS["photo"],
        kind="photo",
    )
    messages = _build_messages(
        settings=settings,
        state=state,
        decision=router.Decision(router.FAST),
        sender="سارا",
        text="این عکسو ببین",
        bundle=bundle,
        is_group=True,
        bot_name="آستولفو",
    )
    assert messages[0]["role"] == "system"
    # پیام فعلی نباید دوبار بیاید
    assert sum(1 for m in messages if isinstance(m["content"], str) and "این عکسو ببین" in m["content"]) == 0
    last = messages[-1]
    assert last["role"] == "user" and isinstance(last["content"], list)
    assert last["content"][0]["type"] == "text" and "سارا:" in last["content"][0]["text"]
    assert last["content"][1]["type"] == "image_url"
    assert "یک استیکر تلگرام است." in last["content"][0]["text"]

    # بدون رسانه → محتوای متنی ساده
    plain = _build_messages(
        settings=settings,
        state=state,
        decision=router.Decision(router.SEARCH, web=True, query="قیمت دلار"),
        sender="سارا",
        text="قیمت دلار؟",
        bundle=MediaBundle(),
        is_group=True,
        bot_name="آستولفو",
    )
    assert isinstance(plain[-1]["content"], str)
    assert any("قیمت دلار" in m["content"] for m in plain if m["role"] == "system")


def test_persona_reinjection():
    settings = _settings(persona_reinject_every=2)
    state = ChatState(chat_id=2, history=collections.deque(maxlen=10))
    state.add_user("رضا", "یک")
    state.add_user("رضا", "دو")  # turn_count = 2 → یادآور تزریق شود
    messages = _build_messages(
        settings=settings,
        state=state,
        decision=router.Decision(router.FAST),
        sender="رضا",
        text="دو",
        bundle=MediaBundle(),
        is_group=True,
        bot_name="آستولفو",
    )
    assert any(m["content"] == persona.SLIM_REMINDER for m in messages)


# ---------------------------------------------------------------------------
def test_memory_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(data_dir=tmp, max_history_len=4)
        store = ChatStore(settings)
        state = store.get(101)
        state.notes = "رضا عاشق قهوه‌ست"
        state.reply_chance = 0.5
        state.forced_mode = "think"
        store.mark_dirty()
        store.save()

        reloaded = ChatStore(settings)
        again = reloaded.get(101)
        assert again.notes == "رضا عاشق قهوه‌ست"
        assert again.reply_chance == 0.5
        assert again.forced_mode == "think"
        assert len(again.history) == 0, "متن پیام‌ها نباید روی دیسک ذخیره شود"


def test_history_bounds_and_participants():
    state = ChatState(chat_id=3, history=collections.deque(maxlen=3))
    for i in range(10):
        state.add_user(f"کاربر{i}", f"پیام {i}")
    assert len(state.history) == 3
    assert len(state.participants) <= 20
    assert state.turn_count == 10
    assert state.recent_texts(2) == ["کاربر8: پیام 8", "کاربر9: پیام 9"]


def test_store_lru_eviction():
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(data_dir=tmp, max_chats=3)
        store = ChatStore(settings)
        for cid in range(6):
            store.get(cid)
        assert len(store.all_states()) <= 3


# ---------------------------------------------------------------------------
def test_loads_loose():
    assert _loads_loose('{"mode":"fast"}')["mode"] == "fast"
    assert _loads_loose('```json\n{"mode":"think"}\n```')["mode"] == "think"
    assert _loads_loose('بله حتماً: {"web": true} امیدوارم کمک کنه')["web"] is True
    assert _loads_loose("چیزی نیست") is None


def test_media_placeholders_cover_kinds():
    for kind in ("photo", "sticker", "animation", "video", "video_note", "voice", "audio"):
        assert kind in PLACEHOLDERS and PLACEHOLDERS[kind].startswith("[")


# ---------------------------------------------------------------------------
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
            print(f"💥 {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} تست پاس شد.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
