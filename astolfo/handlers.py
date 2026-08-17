"""هندلرهای تلگرام: دستورها و جریان اصلی پاسخ‌دهی."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import List, Optional, Tuple

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import ContextTypes

from . import media as media_mod
from . import persona, router
from .ai import AIClient, ChatResult
from .config import Settings
from .memory import ChatState, ChatStore, update_notes
from .utils import (
    clean_name,
    format_sources,
    is_addressed,
    polish,
    split_message,
    typing_indicator,
)

log = logging.getLogger("astolfo.handlers")

HELP_TEXT = (
    "منم آستولفو~ اینا کارهاییه که بلدم:\n\n"
    "• ریپلای یا منشنم کنی همیشه جواب می‌دم، وگرنه گاهی خودم می‌پرم وسط بحث\n"
    "• عکس، استیکر، گیف، ویدیو و ویس رو نگاه/گوش می‌کنم و نظر می‌دم\n"
    "• سؤال سخت باشه فکر می‌کنم، سؤال روز باشه سرچ می‌کنم، گپ ساده باشه سریع جواب می‌دم\n"
    "• فقط متن می‌فرستم؛ عکس و ویس نمی‌سازم\n\n"
    "دستورها:\n"
    "/chance ۰تا۱۰۰ — چقدر خودم بپرم وسط بحث\n"
    "/mode auto|fast|think|search — حالت جواب دادن\n"
    "/reset — فراموش کردن بحث و یادداشت‌ها\n"
    "/mute و /unmute — ساکتم کن / برم گردون\n"
    "/status — وضعیت فعلی"
)

MUTED_MSG = "باشه باشه ساکت شدم 🤐 (با /unmute برم گردون)"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _bits(context: ContextTypes.DEFAULT_TYPE) -> Tuple[Settings, ChatStore, AIClient]:
    data = context.application.bot_data
    return data["settings"], data["store"], data["ai"]


async def _may_configure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """در گروه فقط ادمین‌ها تنظیمات را عوض کنند."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return True
    settings, _, _ = _bits(context)
    if user.id in settings.admin_ids:
        return True
    try:
        member = await chat.get_member(user.id)
        return member.status in {"administrator", "creator"}
    except Exception:
        return False


def _mode_params(settings: Settings, decision: router.Decision, has_media: bool) -> dict:
    """مدل و پارامترهای تولید را بر اساس تصمیم مسیریاب انتخاب می‌کند."""
    if decision.mode == router.THINK:
        model = settings.model_think
        params = {
            "temperature": settings.temperature_think,
            "max_tokens": settings.max_tokens_think,
            "reasoning": {"effort": settings.think_reasoning_effort},
        }
    elif decision.mode == router.SEARCH:
        model = settings.model_search or settings.model_fast
        params = {
            "temperature": settings.temperature_grounded,
            "max_tokens": max(settings.max_tokens_fast, settings.max_tokens_think // 2),
            "reasoning": None,
        }
    elif decision.mode == router.SERIOUS:
        model = settings.model_think
        params = {
            "temperature": 0.6,
            "max_tokens": settings.max_tokens_fast,
            "reasoning": {"effort": "low"},
        }
    else:  # fast
        model = settings.model_fast
        params = {
            "temperature": settings.temperature_fast,
            "max_tokens": settings.max_tokens_fast,
            "reasoning": (
                {"max_tokens": 0} if settings.fast_reasoning_budget <= 0 else
                {"max_tokens": settings.fast_reasoning_budget}
            ),
        }

    # رسانه حتماً باید به مدل چندوجهی برود
    if has_media and decision.mode not in {router.THINK, router.SERIOUS}:
        model = settings.model_media
    params["model"] = model
    return params


def _build_messages(
    *,
    settings: Settings,
    state: ChatState,
    decision: router.Decision,
    sender: str,
    text: str,
    bundle: media_mod.MediaBundle,
    is_group: bool,
    bot_name: str,
) -> List[dict]:
    system_prompt = persona.build_system_prompt(
        mode=decision.mode,
        is_group=is_group,
        has_media=bundle.has_content or bool(bundle.notes),
        notes=state.notes,
        participants=list(state.participants.keys()),
        bot_name=bot_name,
    )
    messages: List[dict] = [{"role": "system", "content": system_prompt}]

    # تاریخچه بدون پیام فعلی (پیام فعلی جداگانه و کامل ساخته می‌شود)
    history = list(state.history)[:-1]
    messages.extend(history)

    # تزریق دوره‌ای یادآور شخصیت برای جلوگیری از افت لحن در چت‌های طولانی
    if settings.persona_reinject_every > 0 and state.turn_count and (
        state.turn_count % settings.persona_reinject_every == 0
    ):
        messages.append({"role": "system", "content": persona.SLIM_REMINDER})

    if decision.mode == router.SEARCH and decision.query:
        messages.append(
            {
                "role": "system",
                "content": (
                    "این نوبت با جست‌وجوی وب همراه است. موضوع جست‌وجو: "
                    f"«{decision.query}». فقط بر اساس نتایج واقعی جواب بده و اگر نتایج "
                    "چیزی نگفتند، صادقانه بگو پیدا نشد."
                ),
            }
        )

    head = f"{sender}: {text}".strip() if text else f"{sender}: {bundle.placeholder}"
    if bundle.notes:
        head += "\n(" + " ".join(bundle.notes) + ")"

    if bundle.has_content:
        messages.append(
            {"role": "user", "content": [{"type": "text", "text": head}, *bundle.parts]}
        )
    else:
        messages.append({"role": "user", "content": head})
    return messages


# ---------------------------------------------------------------------------
# دستورها
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(persona.GREETING)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _may_configure(update, context):
        await update.effective_message.reply_text("این یکی رو فقط ادمین‌ها می‌تونن بزنن~")
        return
    _, store, _ = _bits(context)
    state = store.get(update.effective_chat.id)
    state.history.clear()
    state.notes = ""
    state.participants.clear()
    state.turn_count = 0
    store.mark_dirty()
    await update.effective_message.reply_text("هوپ! همه‌چی از ذهنم پرید 🧹 (که خب... کار سختی نبود)")


async def cmd_chance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, _ = _bits(context)
    state = store.get(update.effective_chat.id)
    current = state.reply_chance if state.reply_chance is not None else settings.group_reply_chance

    if not context.args:
        await update.effective_message.reply_text(
            f"الان {round(current * 100)}٪ مواقع خودم می‌پرم وسط بحث. با /chance 40 عوضش کن."
        )
        return
    if not await _may_configure(update, context):
        await update.effective_message.reply_text("این یکی رو فقط ادمین‌ها می‌تونن بزنن~")
        return
    try:
        value = max(0, min(100, int(context.args[0].strip("%٪"))))
    except ValueError:
        await update.effective_message.reply_text("یه عدد بین ۰ تا ۱۰۰ بده. مثلاً: /chance 40")
        return

    state.reply_chance = value / 100
    store.mark_dirty()
    await update.effective_message.reply_text(f"باشه! از این به بعد {value}٪ مواقع می‌پرم وسط 😌")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, store, _ = _bits(context)
    state = store.get(update.effective_chat.id)
    label = state.forced_mode or "auto"

    if not context.args:
        await update.effective_message.reply_text(
            f"حالت فعلی: {label}\n"
            "auto = خودم تصمیم می‌گیرم، fast = سریع، think = با فکر، search = با سرچ"
        )
        return
    if not await _may_configure(update, context):
        await update.effective_message.reply_text("این یکی رو فقط ادمین‌ها می‌تونن بزنن~")
        return

    choice = context.args[0].lower().strip()
    if choice in {"auto", "خودکار"}:
        state.forced_mode = None
    elif choice in router.VALID_MODES:
        state.forced_mode = choice
    else:
        await update.effective_message.reply_text("یکی از اینا: auto / fast / think / search")
        return
    store.mark_dirty()
    await update.effective_message.reply_text(f"حالت شد {state.forced_mode or 'auto'} ✨")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _may_configure(update, context):
        await update.effective_message.reply_text("این یکی رو فقط ادمین‌ها می‌تونن بزنن~")
        return
    _, store, _ = _bits(context)
    state = store.get(update.effective_chat.id)
    state.muted = True
    store.mark_dirty()
    await update.effective_message.reply_text(MUTED_MSG)


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _may_configure(update, context):
        await update.effective_message.reply_text("این یکی رو فقط ادمین‌ها می‌تونن بزنن~")
        return
    _, store, _ = _bits(context)
    state = store.get(update.effective_chat.id)
    state.muted = False
    store.mark_dirty()
    await update.effective_message.reply_text("برگشتممم! 🎉 دلتون تنگ شده بود نه؟")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, store, ai = _bits(context)
    state = store.get(update.effective_chat.id)
    chance = state.reply_chance if state.reply_chance is not None else settings.group_reply_chance
    await update.effective_message.reply_text(
        "وضعیت من:\n"
        f"• حالت: {state.forced_mode or 'auto'}   • ساکت: {'آره' if state.muted else 'نه'}\n"
        f"• احتمال ورود خودکار: {round(chance * 100)}٪\n"
        f"• پیام‌های تو حافظه: {len(state.history)} از {settings.max_history_len}\n"
        f"• یادداشت بلندمدت: {'دارم' if state.notes else 'ندارم'}\n"
        f"• جواب‌هایی که اینجا دادم: {state.replies_sent}\n"
        f"• مدل سریع: {settings.model_fast}\n"
        f"• مدل فکری: {settings.model_think}\n"
        f"• سرچ وب: {'روشن' if settings.web_search_enabled else 'خاموش'}\n"
        f"• تحلیل ویس/ویدیو: {'کامل' if media_mod.ffmpeg_available() else 'محدود (ffmpeg نیست)'}\n"
        f"• توکن مصرفی این ران: {ai.total_tokens}"
    )


# ---------------------------------------------------------------------------
# جریان اصلی
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.from_user is None:
        return
    if message.from_user.is_bot:  # جلوگیری از حلقهٔ ربات‌ها
        return

    settings, store, ai = _bits(context)
    chat = message.chat
    state = store.get(chat.id)
    if state.muted:
        return
    if chat.title and state.title != chat.title:
        state.title = chat.title
        store.mark_dirty()

    kind, _ = media_mod.detect(message)
    raw_text = (message.text or message.caption or "").strip()
    if not raw_text and not kind:
        return

    sender = clean_name(message.from_user.first_name or message.from_user.username)
    text = raw_text[: settings.max_chars_per_message]

    # هر پیام دقیقاً یک‌بار در تاریخچه ثبت می‌شود، چه جواب بدهیم چه ندهیم
    history_text = text or media_mod.PLACEHOLDERS.get(kind, "[یک فایل فرستاد]")
    if text and kind:
        history_text = f"{text} {media_mod.PLACEHOLDERS.get(kind, '')}".strip()
    state.add_user(sender, history_text)

    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    bot_user = context.bot.bot  # اطلاعات کش‌شدهٔ ربات، بدون درخواست شبکه
    addressed = (not is_group) or is_addressed(message, bot_user)

    if not addressed:
        now = time.monotonic()
        if now - state.last_reply_at < settings.reply_cooldown_sec:
            return
        base = state.reply_chance if state.reply_chance is not None else settings.group_reply_chance
        # به رسانه بیشتر واکنش نشان می‌دهد (مگر اینکه ادمین احتمال را صفر کرده باشد)
        chance = max(base, settings.media_reply_chance) if (kind and base > 0) else base
        if random.random() >= chance:
            return

    if state.lock.locked():  # همین حالا مشغول جواب دادن به این چت هستیم
        return

    async with state.lock:
        state.last_reply_at = time.monotonic()
        action = ChatAction.TYPING
        async with typing_indicator(context.bot, chat.id, action):
            bundle = media_mod.MediaBundle()
            if kind:
                bundle = await media_mod.collect(context.bot, message, settings)

            decision = await router.decide(
                ai,
                settings,
                text=text,
                recent=state.recent_texts(),
                has_media=bool(kind),
                forced_mode=state.forced_mode,
            )
            log.info("چت %s | %s | %s", chat.id, sender, decision)

            params = _mode_params(settings, decision, bundle.has_content)
            messages = _build_messages(
                settings=settings,
                state=state,
                decision=decision,
                sender=sender,
                text=text,
                bundle=bundle,
                is_group=is_group,
                bot_name=(bot_user.first_name if bot_user else "Astolfo"),
            )

            result: ChatResult = await ai.chat(
                messages,
                model=params["model"],
                temperature=params["temperature"],
                max_tokens=params["max_tokens"],
                reasoning=params.get("reasoning"),
                web=decision.web,
            )

        reply = polish(result.text or "")
        if not reply:
            log.warning("پاسخی تولید نشد (%s)", result.error)
            if addressed:
                with contextlib.suppress(Exception):
                    await message.reply_text(
                        "اوه... مغزم یه لحظه رفت رو ماه و برنگشت 😵‍💫 یه بار دیگه بگو؟"
                    )
            return

        state.add_assistant(reply)
        store.mark_dirty()

    if settings.show_sources and result.citations and decision.mode == router.SEARCH:
        reply += format_sources(result.citations)

    no_preview = LinkPreviewOptions(is_disabled=True)
    for chunk in split_message(reply):
        try:
            await message.reply_text(chunk, link_preview_options=no_preview)
        except Exception as exc:
            log.warning("ارسال پیام به چت %s شکست خورد: %s", chat.id, exc)
            break

    # خلاصه‌سازی حافظه در پس‌زمینه تا کاربر منتظر نماند
    if settings.summary_enabled:
        context.application.create_task(update_notes(ai, settings, state))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("خطای پردازش‌نشده", exc_info=context.error)
