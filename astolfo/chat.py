"""Message pipeline: decide whether to answer, how hard to think, and what to send."""

from __future__ import annotations

import logging
import random
import time

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import ContextTypes

from . import media as media_mod
from . import persona, runtime
from .budget import STOPPED, Allowance
from .cache import normalize
from .llm import ChatResult, Usage, cacheable_system
from .memory import ChatState, update_notes
from .persona import FAST, SEARCH, SERIOUS, THINK
from .routing import Decision
from .runtime import Runtime
from .text import (
    clean_name,
    format_sources,
    is_addressed,
    polish,
    split_message,
    typing_indicator,
)

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def model_params(settings, decision: Decision, has_media: bool) -> dict:
    """Model, temperature, token ceiling and reasoning budget for a decision."""
    if decision.mode == THINK:
        params = {
            "model": settings.model_think,
            "temperature": settings.temperature_think,
            "max_tokens": settings.max_tokens_think,
            "reasoning": {"effort": settings.think_effort},
        }
    elif decision.mode == SEARCH:
        params = {
            "model": settings.model_search or settings.model_fast,
            "temperature": settings.temperature_grounded,
            "max_tokens": max(settings.max_tokens_fast, settings.max_tokens_think // 2),
            "reasoning": None,
        }
    elif decision.mode == SERIOUS:
        params = {
            "model": settings.model_think,
            "temperature": 0.6,
            "max_tokens": settings.max_tokens_fast,
            "reasoning": {"effort": "low"},
        }
    else:
        params = {
            "model": settings.model_fast,
            "temperature": settings.temperature_fast,
            "max_tokens": settings.max_tokens_fast,
            "reasoning": {"max_tokens": max(0, settings.fast_reasoning_budget)},
        }

    if has_media and decision.mode not in {THINK, SERIOUS}:
        params["model"] = settings.model_media
    return params


def resolve_locale(rt: Runtime, state: ChatState) -> str:
    if rt.settings.persona_locale in {"en", "fa"}:
        return rt.settings.persona_locale
    if state.locale:
        return state.locale
    detected = persona.detect_locale(state.recent_texts(6), default=rt.settings.locale)
    state.locale = detected
    rt.store.mark_dirty()
    return detected


def build_messages(
    rt: Runtime,
    state: ChatState,
    *,
    decision: Decision,
    sender: str,
    text: str,
    bundle: media_mod.MediaBundle,
    is_group: bool,
    bot_name: str,
    model: str,
) -> list[dict]:
    settings = rt.settings
    has_media = bundle.has_content or bool(bundle.notes)

    static_block = persona.static_prompt(is_group=is_group, locale=resolve_locale(rt, state))
    messages: list[dict] = [
        cacheable_system(static_block, model, settings.prompt_cache_control),
        {
            "role": "system",
            "content": persona.dynamic_prompt(
                mode=decision.mode,
                has_media=has_media,
                notes=state.notes,
                participants=list(state.participants),
                bot_name=bot_name,
                search_query=decision.query if decision.mode == SEARCH else None,
            ),
        },
    ]
    messages.extend(state.prompt_history(settings.history_char_budget))

    every = settings.persona_reinject_every
    if every > 0 and state.turn_count and state.turn_count % every == 0:
        messages.append({"role": "system", "content": persona.REMINDER})

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


async def send_reply(message, text: str) -> None:
    for chunk in split_message(text):
        try:
            await message.reply_text(chunk, link_preview_options=NO_PREVIEW)
        except Exception as exc:
            log.warning("could not send reply to chat %s: %s", message.chat_id, exc)
            return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.from_user is None or message.from_user.is_bot:
        return

    rt = runtime.get(context)
    settings = rt.settings
    chat = message.chat
    state = rt.store.get(chat.id)
    if state.muted:
        return
    if chat.title and state.title != chat.title:
        state.title = chat.title
        rt.store.mark_dirty()

    kind, _ = media_mod.detect(message)
    raw_text = (message.text or message.caption or "").strip()
    if not raw_text and not kind:
        return

    sender = clean_name(message.from_user.first_name or message.from_user.username)
    text = raw_text[: settings.max_input_chars]
    placeholder = media_mod.PLACEHOLDERS.get(kind, "[sent a file]") if kind else ""
    state.add_user(sender, f"{text} {placeholder}".strip() if kind else text)

    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    bot_user = context.bot.bot
    addressed = (not is_group) or is_addressed(message, bot_user)

    allowance = rt.budget.check(chat_id=chat.id, addressed=addressed)
    if not allowance.allowed:
        log.info("chat %s skipped: %s", chat.id, allowance.reason)
        # Tell the chat once an hour instead of on every message.
        now = time.monotonic()
        if addressed and allowance.level == STOPPED and now - state.budget_notice_at > 3600:
            state.budget_notice_at = now
            await send_reply(message, rt.strings("budget_stopped"))
        return

    if not addressed and not _should_join(rt, state, kind):
        return
    if state.lock.locked():  # already composing a reply for this chat
        return

    async with state.lock:
        state.last_reply_at = time.monotonic()

        cache_key = (chat.id, normalize(text))
        if settings.response_cache and not kind:
            cached = rt.responses.get(cache_key)
            if cached:
                rt.budget.record_cache_hit()
                state.add_assistant(cached)
                log.info("chat %s served from response cache", chat.id)
                await send_reply(message, cached)
                return

        async with typing_indicator(context.bot, chat.id, ChatAction.TYPING):
            bundle = (
                await media_mod.collect(context.bot, message, settings)
                if kind
                else media_mod.MediaBundle()
            )
            decision, router_usage = await rt.router.decide(
                text=text,
                recent=state.recent_texts(),
                has_media=bool(kind),
                forced_mode=state.forced_mode or allowance.force_mode,
                forced_source="user" if state.forced_mode else "budget",
                allow_llm=allowance.allow_router_llm,
            )
            rt.record(mode="router", model=settings.model_router, usage=router_usage,
                      chat_id=chat.id)

            if not allowance.allow_web and decision.web:
                decision = Decision(decision.mode, False, decision.source, "web disabled by budget")

            params = model_params(settings, decision, bundle.has_content)
            log.info("chat %s | %s | %s | %s", chat.id, sender, decision, params["model"])

            messages = build_messages(
                rt,
                state,
                decision=decision,
                sender=sender,
                text=text,
                bundle=bundle,
                is_group=is_group,
                bot_name=(bot_user.first_name if bot_user else "Astolfo"),
                model=params["model"],
            )
            result: ChatResult = await rt.llm.chat(
                messages,
                model=params["model"],
                temperature=params["temperature"],
                max_tokens=params["max_tokens"],
                reasoning=params["reasoning"],
                web=decision.web,
            )
            rt.record(mode=decision.mode, model=result.model or params["model"],
                      usage=result.usage, chat_id=chat.id)

        reply = polish(result.text or "")
        if not reply:
            log.warning("no completion for chat %s: %s", chat.id, result.error)
            if addressed:
                await send_reply(message, rt.strings("error_reply"))
            return

        state.add_assistant(reply)
        rt.store.mark_dirty()
        if settings.response_cache and not kind and not decision.web and decision.mode == FAST:
            rt.responses.set(cache_key, reply)

    if settings.show_sources and result.citations and decision.mode == SEARCH:
        reply += format_sources(result.citations)

    await send_reply(message, reply)

    if settings.summaries:
        context.application.create_task(_summarize(rt, state))


def _should_join(rt: Runtime, state: ChatState, kind: str) -> bool:
    """Random participation, throttled by a cooldown."""
    settings = rt.settings
    if time.monotonic() - state.last_reply_at < settings.reply_cooldown:
        return False
    base = state.reply_chance if state.reply_chance is not None else settings.group_reply_chance
    if base <= 0:
        return False
    chance = max(base, settings.media_reply_chance) if kind else base
    return random.random() < chance


async def _summarize(rt: Runtime, state: ChatState) -> None:
    usage: Usage = await update_notes(rt.llm, rt.settings, state)
    rt.record(mode="summary", model=rt.settings.model_summary, usage=usage, chat_id=state.chat_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error", exc_info=context.error)


__all__ = ["handle_message", "on_error", "model_params", "build_messages", "Allowance"]
