"""Command handlers."""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from . import branding, media, runtime
from .budget import FULL
from .routing import MODES

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("start", "say hi to Astolfo"),
    BotCommand("help", "what the bot can do"),
    BotCommand("about", "channel, creator and what I am"),
    BotCommand("chance", "auto-join chance, 0-100"),
    BotCommand("mode", "auto | fast | think | search"),
    BotCommand("usage", "credit usage and cost"),
    BotCommand("donate", "feed Astolfo with Telegram Stars"),
    BotCommand("status", "current settings"),
    BotCommand("reset", "clear this chat's memory"),
    BotCommand("mute", "go quiet"),
    BotCommand("unmute", "come back"),
]


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return True
    if user.id in runtime.get(context).settings.admin_ids:
        return True
    try:
        member = await chat.get_member(user.id)
    except Exception:
        return False
    return member.status in {"administrator", "creator"}


async def _deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(runtime.get(context).strings("admin_only"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    await update.effective_message.reply_text(
        f"{rt.strings('greeting')}\n\n{branding.credit(rt.strings.locale)}"
    )


async def help_(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    await update.effective_message.reply_text(
        f"{rt.strings('help')}\n\n{branding.credit(rt.strings.locale)}"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    await update.effective_message.reply_text(branding.about(rt.strings.locale))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return await _deny(update, context)
    rt = runtime.get(context)
    state = rt.store.get(update.effective_chat.id)
    state.history.clear()
    state.participants.clear()
    state.notes = ""
    state.turn_count = 0
    rt.store.mark_dirty()
    await update.effective_message.reply_text(rt.strings("reset_done"))


async def chance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    state = rt.store.get(update.effective_chat.id)
    current = state.reply_chance
    if current is None:
        current = rt.settings.group_reply_chance

    if not context.args:
        await update.effective_message.reply_text(
            rt.strings("chance_current", percent=round(current * 100))
        )
        return
    if not await _is_admin(update, context):
        return await _deny(update, context)

    try:
        value = max(0, min(100, int(context.args[0].strip("%٪"))))
    except ValueError:
        await update.effective_message.reply_text(rt.strings("chance_bad"))
        return

    state.reply_chance = value / 100
    rt.store.mark_dirty()
    await update.effective_message.reply_text(rt.strings("chance_set", percent=value))


async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    state = rt.store.get(update.effective_chat.id)

    if not context.args:
        await update.effective_message.reply_text(
            rt.strings("mode_current", mode=state.forced_mode or "auto")
        )
        return
    if not await _is_admin(update, context):
        return await _deny(update, context)

    choice = context.args[0].lower().strip()
    if choice == "auto":
        state.forced_mode = None
    elif choice in MODES:
        state.forced_mode = choice
    else:
        await update.effective_message.reply_text(rt.strings("mode_bad"))
        return
    rt.store.mark_dirty()
    await update.effective_message.reply_text(
        rt.strings("mode_set", mode=state.forced_mode or "auto")
    )


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return await _deny(update, context)
    rt = runtime.get(context)
    rt.store.get(update.effective_chat.id).muted = True
    rt.store.mark_dirty()
    await update.effective_message.reply_text(rt.strings("muted"))


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return await _deny(update, context)
    rt = runtime.get(context)
    rt.store.get(update.effective_chat.id).muted = False
    rt.store.mark_dirty()
    await update.effective_message.reply_text(rt.strings("unmuted"))


def _billing_label(rt) -> str:
    services = ", ".join(p.name for p in rt.llm.providers)
    if not rt.settings.free_mode:
        return f"paid models via {services}"
    pool = rt.llm.free_pool()
    vision = "with images" if rt.llm.supports_free_vision() else "text only"
    return f"free models via {services} ({len(pool)} available, {vision})"


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    settings = rt.settings
    state = rt.store.get(update.effective_chat.id)
    chance_value = state.reply_chance
    if chance_value is None:
        chance_value = settings.group_reply_chance

    await update.effective_message.reply_text(
        rt.strings(
            "status",
            mode=state.forced_mode or "auto",
            muted="yes" if state.muted else "no",
            chance=round(chance_value * 100),
            history=len(state.history),
            max_history=settings.max_history,
            notes="yes" if state.notes else "no",
            replies=state.replies_sent,
            billing=_billing_label(rt),
            model_fast=settings.model_fast,
            model_think=settings.model_think,
            web="on" if settings.web_search else "off",
            ffmpeg="full" if media.ffmpeg_available() else "limited (no ffmpeg)",
        )
    )


async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    summary = rt.budget.summary()
    allowance = rt.budget.check(chat_id=update.effective_chat.id, addressed=True)

    budget_note = ""
    if summary["daily_budget"] > 0:
        share = summary["cost_today"] / summary["daily_budget"] * 100
        budget_note = f"of ${summary['daily_budget']:.2f} ({share:.0f}%)"

    by_mode = ", ".join(f"{k} ${v:.4f}" for k, v in summary["by_mode"].items()) or "-"
    by_service = (
        ", ".join(
            f"{name} {row['requests']}✓"
            + (f"/{row['failures']}✗" if row["failures"] else "")
            for name, row in rt.registry.usage_today().items()
        )
        or "-"
    )
    # Cost separates paid models; on free ones only the work does, so show both.
    by_model = (
        ", ".join(
            f"{model.split('/', 1)[-1]} {row['calls']}× {row['prompt'] + row['completion']}t"
            for model, row in summary["by_model"][:3]
        )
        or "-"
    )
    router_hits = rt.router.cache.hits

    await update.effective_message.reply_text(
        rt.strings(
            "usage",
            cost_today=f"{summary['cost_today']:.4f}",
            cost_month=f"{summary['cost_month']:.4f}",
            budget_note=budget_note,
            calls=summary["calls"],
            prompt_tokens=summary["prompt_tokens"],
            completion_tokens=summary["completion_tokens"],
            cache_hit_rate=f"{summary['cache_hit_rate'] * 100:.0f}%",
            cached_tokens=summary["cached_tokens"],
            cache_replies=summary["cache_replies"],
            stars_today=summary["stars_today"],
            router_saved=router_hits,
            by_mode=by_mode,
            by_model=by_model,
            by_service=by_service,
            level="normal" if allowance.level == FULL else allowance.level,
        )
    )
