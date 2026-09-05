"""Message pipeline: decide whether to answer, how hard to think, and what to send."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import NamedTuple

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import ContextTypes

from . import media as media_mod
from . import offline, participation, persona, recipes, roles, runtime, tuning
from .budget import STOPPED, Allowance
from .cache import normalize
from .llm import ChatResult, Usage, cacheable_system
from .memory import MAX_WAITING, ChatState, composing, history_budget, update_notes
from .persona import FAST, SEARCH, SERIOUS, THINK
from .routing import Decision
from .runtime import Runtime
from .text import (
    RECENT_HEARD,
    RECENT_REPLIES,
    clean_name,
    cut_impersonation,
    drop_translation,
    format_sources,
    has_slur,
    is_addressed,
    looks_broken,
    normalize_input,
    polish,
    shorten,
    split_message,
    strip_speaker,
    typing_indicator,
    went_explicit,
)

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# When the provider is down every addressed message would otherwise get its own
# apology, which reads as the bot spamming the chat.
ERROR_NOTICE_INTERVAL = 120.0

# Enough of the quoted message to know what is being answered, not enough to pay
# for someone's essay twice.
QUOTE_CHARS = 140

# The two shapes the system prompt comes in. Recorded against every reply, so
# that how each one fares on each model is measured before anything chooses
# between them.
COMPACT = persona.COMPACT
LAYERED = persona.FULL


def recipe_for(rt: Runtime, state: ChatState, model: str) -> recipes.Recipe:
    """The recipe this turn is built from.

    Three things decide it, in this order. A weight chosen by hand in the panel
    wins outright - it is a person overruling everything. Otherwise the brain
    picks, which with BRAIN off is the recipe the bot has always used. Then the
    chat's own mood is laid on top, because a mood is about this chat and the
    recipe is about the model.
    """
    settings = rt.settings
    wanted = (settings.prompt_tier or persona.AUTO).strip().lower()
    if wanted in persona.TIERS:
        chosen = recipes.FACTORY[wanted]
    else:
        chosen = rt.brain.choose(model=model, free_mode=settings.free_mode)
    mood = state.mood.now()
    return chosen if mood == persona.BRIGHT else replace(chosen, mood=mood)


def prompt_variant(settings) -> str:
    """Which of the three prompt weights a turn will use.

    Measured, not guessed: the full prompt is ~4,600 tokens over 52 separate
    rules and the compact one ~1,080 over about thirty, and a 35B model handed
    thirty rules follows some and drops the rest. So there is a third, lighter
    weight, and a setting that picks one by hand.

    `auto` is what the bot has always done - the short prompt on free models,
    the long one otherwise - and it is still the default. It is also the hook
    the brain takes over: choosing the weight per model family is the job, and
    until it can, this is a person choosing it.
    """
    wanted = (getattr(settings, "prompt_tier", "") or persona.AUTO).strip().lower()
    if wanted in persona.TIERS:
        return wanted
    return COMPACT if settings.free_mode else LAYERED


def static_block_for(variant: str, *, is_group: bool, locale: str, heavy_lifting: bool) -> str:
    """The persona at the chosen weight."""
    if variant == persona.TIGHT:
        return persona.tight_prompt(is_group=is_group, locale=locale)
    if variant == COMPACT:
        return persona.compact_prompt(is_group=is_group, locale=locale)
    return persona.static_prompt(
        is_group=is_group, locale=locale, heavy_lifting=heavy_lifting
    )


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


class ReplyTarget(NamedTuple):
    """Who the newest message is answering, and what they had said."""

    who: str
    quote: str


def reply_target(message, bot_user) -> ReplyTarget | None:
    """Read Telegram's own reply link, which the prompt never used to carry.

    Two people holding separate conversations in one group produced one flat
    transcript, so the bot answered whoever spoke last about whatever was
    loudest, and the person it was replying to watched it wander off. Telegram
    already knows which message this one is aimed at.
    """
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return None

    author = getattr(replied, "from_user", None)
    if author is None:
        who = "an earlier message"
    elif bot_user is not None and author.id == bot_user.id:
        who = "you"
    else:
        who = clean_name(author.first_name or author.username)

    said = (getattr(replied, "text", "") or getattr(replied, "caption", "") or "").strip()
    if not said:
        kind, _ = media_mod.detect(replied)
        said = media_mod.PLACEHOLDERS.get(kind, "") if kind else ""
    return ReplyTarget(who, shorten(normalize_input(said), QUOTE_CHARS))


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
    reply_tokens: int = 0,
    answering: ReplyTarget | None = None,
    standing: str = "",
) -> list[dict]:
    settings = rt.settings
    has_media = bundle.has_content or bool(bundle.notes)

    locale = resolve_locale(rt, state)
    recipe = recipe_for(rt, state, model)
    # The media block has its own two weights, and anything but the full prompt
    # takes the short one: a model that drowns in the persona drowns in this too.
    compact = recipe.short
    static_block = recipe.render(
        is_group=is_group, locale=locale, heavy_lifting=settings.heavy_lifting
    )
    dynamic_block = persona.dynamic_prompt(
        mode=decision.mode,
        has_media=has_media,
        notes=state.notes,
        # Every line of the transcript below already starts with a name, so the
        # list is only worth its tokens before there is a transcript. It also
        # stopped helping: naming people who are not in the window is half of
        # why replies used to wander off to whoever was not talking.
        participants=list(state.participants) if len(state.history) <= 1 else None,
        bot_name=bot_name,
        sender=sender,
        search_query=decision.query if decision.mode == SEARCH else None,
        style=state.style.for_turn(sender, answering.who if answering else ""),
        threaded=answering is not None,
        compact=compact,
        standing=standing or None,
        busy_elsewhere=rt.attention.elsewhere(state.chat_id),
        brevity=tuning.brevity_hint(state) if settings.adaptive_length else None,
    )
    messages: list[dict] = [
        cacheable_system(static_block, model, settings.prompt_cache_control),
        {"role": "system", "content": dynamic_block},
    ]

    # What fits, not what was asked for: the same setting used to be sent to a
    # model with 8k of context and one with a million.
    budget = history_budget(
        settings.history_char_budget,
        context_tokens=rt.llm.context_window(rt.llm.resolve(model)),
        overhead_chars=len(static_block) + len(dynamic_block) + len(text) + 400,
        reply_tokens=reply_tokens or settings.max_tokens_fast,
    )
    messages.extend(state.prompt_history(budget))

    every = settings.persona_reinject_every
    if every > 0 and state.turn_count and state.turn_count % every == 0:
        messages.append({"role": "system", "content": persona.REMINDER})

    who = f"{sender} → {answering.who}" if answering else sender
    head = f"{who}: {text}".strip() if text else f"{who}: {bundle.placeholder}"
    if answering and answering.quote:
        # After the message rather than before it: the last thing a small model
        # reads is the thing it answers, and this is the subject, not the ask.
        head += f'\n({answering.who} had said: "{answering.quote}")'
    if bundle.notes:
        head += "\n(" + " ".join(bundle.notes) + ")"

    if bundle.has_content:
        messages.append(
            {"role": "user", "content": [{"type": "text", "text": head}, *bundle.parts]}
        )
    else:
        messages.append({"role": "user", "content": head})
    return messages


# Telegram's wording when the bot is in a group it may not post in.
NO_RIGHTS = ("not enough rights", "have no rights", "chat_write_forbidden")


async def send_reply(message, text: str, rt: Runtime | None = None) -> None:
    """Send a reply, and stop talking to a chat that will not let it.

    A group where the bot lacks permission used to cost a model call per message
    forever: the send failed, the failure was logged, and the next message did it
    all again. Twenty-one of those in one log. The first refusal now switches the
    chat off exactly as the panel would, so it costs nothing until somebody fixes
    the permission and turns it back on.
    """
    for chunk in split_message(text):
        try:
            await message.reply_text(chunk, link_preview_options=NO_PREVIEW)
        except Exception as exc:
            log.warning("could not send reply to chat %s: %s", message.chat_id, exc)
            reason = str(exc).lower()
            if rt is not None and any(mark in reason for mark in NO_RIGHTS):
                rt.set_chat_off(message.chat_id, True)
                rt.db.record(
                    actor=None,
                    action="chat_muted_no_rights",
                    detail=f"{message.chat_id}: {exc}"[:200],
                )
                log.warning(
                    "chat %s will not let the bot post; switching it off until "
                    "somebody grants the permission and turns it back on",
                    message.chat_id,
                )
            return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.from_user is None or message.from_user.is_bot:
        return

    rt = runtime.get(context)
    settings = rt.settings
    chat = message.chat
    if message.from_user.id in rt.blocked:
        return
    state = rt.store.get(chat.id)
    if state.off or state.muted:
        # Dormant is stronger than muted and is checked the same way: before a
        # single word of this message is read, stored or counted.
        return
    if chat.title and state.title != chat.title:
        state.title = chat.title
        rt.store.mark_dirty()

    kind, _ = media_mod.detect(message)
    raw_text = (message.text or message.caption or "").strip()
    if not raw_text and not kind:
        return

    sender = clean_name(message.from_user.first_name or message.from_user.username)
    # Normalised on the way in, once: everything downstream - the prompt, the
    # history, the response cache key - then sees one spelling of a word instead
    # of the four a phone keyboard can produce.
    text = normalize_input(raw_text)[: settings.max_input_chars]

    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    bot_user = context.bot.bot
    addressed = (not is_group) or is_addressed(message, bot_user)
    answering = reply_target(message, bot_user) if is_group else None

    # Read before it is consumed below: two of its own messages in a row is a
    # monologue, and the accounting clears the same flag.
    spoke_last = state.awaiting_reply
    # Whether the last thing it said landed. One shot per reply, and the only
    # signal it has that people here read what it writes.
    if state.awaiting_reply:
        earned = state.note_reception(answered=addressed)
        if addressed:
            rt.credit_answer(earned)
    if addressed:
        # Being spoken to ends the daydream rather than moving it elsewhere.
        rt.attention.release(state.chat_id)

    placeholder = media_mod.PLACEHOLDERS.get(kind, "[sent a file]") if kind else ""
    state.add_user(
        sender,
        f"{text} {placeholder}".strip() if kind else text,
        answering=answering.who if answering else "",
    )
    _track(rt, message, sender)

    if has_slur(text):
        # Answered without a model at all. A member typed a racial slur and the
        # bot transliterated it into Persian and gave it back to the group, and
        # no guard on the reply can be trusted to catch that reliably: the same
        # syllables are ordinary Persian for "look". So the message is where it
        # stops - it gets Astolfo being bored, and no model ever sees it.
        log.warning("chat %s: a message carried a slur; answering without a model", chat.id)
        if addressed:
            async with composing(state):
                state.last_reply_at = time.monotonic()
                said = persona.deflection(resolve_locale(rt, state), state.turn_count)
                state.add_assistant(said)
            await send_reply(message, said, rt)
        return

    allowance = rt.budget.check(
        chat_id=chat.id,
        addressed=addressed,
        user_id=message.from_user.id,
        chat_limit=state.daily_limit,
        user_limit=rt.limit_for(message.from_user.id),
    )
    if not allowance.allowed:
        log.info("chat %s skipped: %s", chat.id, allowance.reason)
        # Tell the chat once an hour instead of on every message.
        now = time.monotonic()
        if addressed and allowance.level == STOPPED and now - state.budget_notice_at > 3600:
            state.budget_notice_at = now
            await send_reply(message, rt.strings("budget_stopped"), rt)
        return

    if not addressed and not participation.should_join(
        rt,
        state,
        kind,
        text=text,
        # A reply between two other people is their conversation, not an opening.
        in_thread=answering is not None and answering.who != "you",
        spoke_last=spoke_last,
    ):
        return
    if state.lock.locked() and (not addressed or state.waiting >= MAX_WAITING):
        # Unprompted chatter arriving mid-reply is noise and is dropped; being
        # spoken to waits its turn instead, up to a point.
        log.info("chat %s is busy, dropping this one", chat.id)
        return

    if addressed and not kind and not rt.llm.usable_now():
        # Every service is resting. Answer what needs no model and skip the rest
        # rather than spending a turn discovering that again.
        async with composing(state):
            state.last_reply_at = time.monotonic()
            said = offline.answer(text, locale=resolve_locale(rt, state))
            state.add_assistant(said or "")
        await send_reply(message, said or _offline_excuse(rt, state), rt)
        return

    async with composing(state):
        state.last_reply_at = time.monotonic()
        # Empty unless a model produces the reply: a cached or offline answer
        # earns nothing for anybody.
        credit = tuning.Credit()

        cache_key = (chat.id, normalize(text))
        if settings.response_cache and not kind:
            cached = rt.responses.get(cache_key)
            if cached:
                rt.budget.record_cache_hit()
                state.add_assistant(cached)
                log.info("chat %s served from response cache", chat.id)
                await send_reply(message, cached, rt)
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
                      chat_id=chat.id, user_id=message.from_user.id)

            if not allowance.allow_web and decision.web:
                decision = Decision(decision.mode, False, decision.source, "web disabled by budget")

            if bundle.has_content and not _can_read_media(rt, bundle):
                # A text-only model would reject the parts outright; say so instead.
                bundle.parts.clear()
                bundle.notes.append(
                    "You cannot see attachments right now because the bot is running on a "
                    "text-only model. Say so honestly in one line and ask what is in it."
                )
                log.info("chat %s: media dropped, no vision model available", chat.id)

            params = model_params(settings, decision, bundle.has_content)
            if settings.adaptive_length:
                # What this chat answers, and what today's budget can still afford.
                params["max_tokens"] = tuning.reply_ceiling(
                    rt, state, base=params["max_tokens"], mode_is_fast=decision.mode == FAST
                )
            # What we intend to ask for. Which model actually answers is only known
            # afterwards - failover can walk past three exhausted services first -
            # so this is logged at debug and the truth is logged below.
            effective = rt.llm.resolve(
                params["model"],
                vision=any(p.get("type") == "image_url" for p in bundle.parts),
                audio=any(p.get("type") == "input_audio" for p in bundle.parts),
            )
            log.debug("chat %s | %s | %s | asking %s", chat.id, sender, decision, effective)

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
                reply_tokens=params["max_tokens"],
                answering=answering,
                standing=await _standing(rt, context, message, sender) if is_group else "",
            )
            call = {
                "model": params["model"],
                "temperature": params["temperature"],
                "max_tokens": params["max_tokens"],
                "reasoning": params["reasoning"],
                "web": decision.web,
            }
            # The same recipe `build_messages` used: what the turn was actually
            # built from is what its outcome has to be credited to.
            recipe = recipe_for(rt, state, params["model"])
            variant = recipe.name
            speakers = _speakers(state, sender, bot_user)

            result: ChatResult = await rt.llm.chat(messages, **call)
            _log_answer(chat.id, sender, decision, result, wanted=effective)
            shaped = _shape(result.text, speakers)
            # Whether it arrived usable is judged for every turn now, not only in
            # free mode: the retry below is still free mode's, but the evidence
            # belongs to whichever model produced it.
            fault = _fault(result, shaped, text, state)
            rt.record(mode=decision.mode, model=result.model or params["model"],
                      usage=result.usage, chat_id=chat.id, user_id=message.from_user.id,
                      service=result.service, variant=variant,
                      latency_ms=result.latency_ms, repaired=shaped.repaired, broken=fault)
            _teach(rt, result, params, recipe, shaped, fault)
            reply = shaped.text

            # Small models leak the prompt or parrot the question. One more go on a
            # different model is cheaper than sending that to the chat.
            if settings.free_mode and fault and rt.llm.stuck_on(result.model):
                # One model left, so "try another" would try the same one. Rest
                # it and send nothing rather than spend a second call on the
                # identical answer.
                log.info("chat %s: %s returned a reply that %s, and it is the only "
                         "model left", chat.id, result.model, fault)
                rt.llm.mark_unusable(result.model)
                reply = ""
            elif settings.free_mode and fault:
                log.info("chat %s: %s returned a reply that %s, retrying",
                         chat.id, result.model, fault)
                rt.llm.mark_unusable(result.model)
                result = await rt.llm.chat(messages, **call)
                shaped = _shape(result.text, speakers)
                fault = _fault(result, shaped, text, state)
                rt.record(mode=decision.mode, model=result.model or params["model"],
                          usage=result.usage, chat_id=chat.id,
                          user_id=message.from_user.id, service=result.service,
                          variant=variant, latency_ms=result.latency_ms,
                          repaired=shaped.repaired, broken=fault)
                _teach(rt, result, params, recipe, shaped, fault)
                reply = shaped.text
                if fault:
                    rt.llm.mark_unusable(result.model)
                    reply = ""

            if reply:
                # Carried with the reply so that an answer to it, which only
                # arrives next turn, lands on what actually produced it.
                credit = tuning.Credit(
                    service=result.service,
                    model=result.model or params["model"],
                    variant=variant,
                    mode=decision.mode,
                )

            if reply and has_slur(reply):
                # It transliterated a racial slur out of the message it was
                # answering and sent it to the group. Never repeated, in any
                # script, whether it thought of it or was handed it.
                log.warning("chat %s: reply carried a slur, deflecting instead", chat.id)
                rt.llm.mark_unusable(result.model)
                reply = persona.deflection(resolve_locale(rt, state), state.turn_count)

            if reply and went_explicit(reply):
                # Not a retry: another model would answer the same question the
                # same way, and the reply we want is the one the prompt asks for
                # anyway. Sent as itself getting bored, never as a refusal notice.
                log.warning("chat %s: reply from %s was explicit, deflecting instead",
                            chat.id, result.model)
                reply = persona.deflection(resolve_locale(rt, state), state.turn_count)

        if not reply:
            log.warning("no completion for chat %s: %s", chat.id, result.error)
            if not addressed:
                return
            # No model answered. Some things need no model at all, and saying one
            # of those is better than an apology.
            reply = offline.answer(text, locale=resolve_locale(rt, state))
            if reply:
                log.info("chat %s answered without a model", chat.id)
            else:
                await _announce_failure(rt, state, message, result)
                return

        state.add_assistant(reply, credit)
        if not addressed:
            # It walked into this conversation on its own, so it is now the one it
            # is thinking about, and the other groups get less of it for a while.
            rt.attention.claim(chat.id)
        rt.store.mark_dirty()
        rt.db.count_reply(chat.id)
        if settings.response_cache and not kind and not decision.web and decision.mode == FAST:
            rt.responses.set(cache_key, reply)

    if settings.show_sources and result.citations and decision.mode == SEARCH:
        reply += format_sources(result.citations)

    await send_reply(message, reply, rt)

    if settings.summaries:
        context.application.create_task(_summarize(rt, state))


def _speakers(state: ChatState, sender: str, bot_user) -> list[str]:
    """Every name this chat writes at the front of a line."""
    names = [sender, *state.participants]
    if bot_user is not None:
        names += [bot_user.first_name or "", getattr(bot_user, "username", "") or ""]
    return [name for name in names if name]


class Shaped(NamedTuple):
    """A reply, and whether it had to be repaired to become one."""

    text: str
    repaired: bool


def _log_answer(chat_id: int, sender: str, decision, result: ChatResult, *, wanted: str) -> None:
    """One line per turn, naming the model that actually answered.

    This used to be written before the call, so it named the model we meant to
    ask for. With three services out of allowance the answer came from the fourth,
    and every line in the log still said the first - which is a good way to spend
    an afternoon blaming the wrong model for somebody else's replies.
    """
    if not result.ok:
        return  # the failure path logs its own reason with the error
    ran = f"{result.service}/{result.model}" if result.service else result.model
    detour = f" (asked {wanted})" if result.model and result.model != wanted else ""
    log.info("chat %s | %s | %s | %s%s", chat_id, sender, decision, ran, detour)


def _teach(rt: Runtime, result: ChatResult, params: dict, recipe, shaped, fault: str) -> None:
    """One turn's outcome into the brain, whether or not the brain is switched on.

    With it off this builds the factory baseline the breaker measures against,
    which is why turning the switch on is not starting from nothing. Whether a
    human answered is not known yet - that arrives next turn, through
    `tuning.Credit` - so this is the half that is knowable now.
    """
    if not result.ok:
        return  # a failed call says nothing about the prompt
    rt.brain.note(
        model=result.model or params.get("model", ""),
        recipe=recipe,
        free_mode=rt.settings.free_mode,
        chars=len(shaped.text or ""),
        repaired=shaped.repaired,
        broken=bool(fault),
        tokens=result.usage.completion_tokens,
        ceiling=int(params.get("max_tokens") or 0),
    )


def _fault(result: ChatResult, shaped: Shaped, echoes: str, state: ChatState) -> str:
    """Why this reply is unusable, or "" when it is fine or never arrived.

    Judged on every turn now, not only in free mode. The retry it feeds is still
    free mode's, but which prompts a model breaks under is worth knowing either
    way, and it was going to a log line and nowhere else.
    """
    if not result.ok:
        return ""  # nothing came back; that is a failed call, not a bad reply
    said = _recent_replies(state)
    return looks_broken(
        shaped.text,
        echoes=echoes,
        previous=said[0] if said else "",
        recent=said[1:],
        heard=_recent_heard(state),
        asked=echoes,
    ) or ""


def _shape(raw: str | None, speakers: list[str]) -> Shaped:
    """Turn what the model returned into one message from one person.

    Shown a transcript, a small model continues it: the reply arrives wearing the
    name of whoever it is answering, and sometimes carries two or three more
    invented turns with real members' names on them. The prompt forbids both;
    this is what catches the ones that do it anyway.

    It also reports whether it had to step in. That used to happen silently, and
    it is the clearest evidence there is that a prompt does not suit a model -
    evidence the bot was throwing away on every message.
    """
    body = raw or ""
    stripped = strip_speaker(body, speakers)
    cut = cut_impersonation(stripped, speakers)
    plain = drop_translation(cut)
    repaired = stripped != body.lstrip() or cut != stripped or plain != cut
    return Shaped(polish(plain), repaired)


def _offline_excuse(rt: Runtime, state: ChatState) -> str:
    return offline.excuse(resolve_locale(rt, state))


async def _standing(rt: Runtime, context, message, sender: str) -> str:
    """Who runs this group and what the bot is in it, for the prompt.

    Best effort by design: it is one line of context, so a group that will not
    tell us simply gets no line rather than a failed turn.
    """
    if not rt.settings.read_admins:
        return ""
    bot_user = context.bot.bot
    roster = await rt.roles.of(
        context.bot, message.chat.id, bot_id=bot_user.id if bot_user else None
    )
    return roles.standing(roster, sender_id=message.from_user.id, sender=sender)


def _track(rt: Runtime, message, sender: str) -> None:
    """Note who is talking where. Counts only, never the text itself."""
    chat, user = message.chat, message.from_user
    try:
        rt.db.seen_chat(
            chat.id,
            kind=str(chat.type),
            # A private chat has no title, so without the name it is a bare id
            # on every screen of the panel.
            title=chat.title or getattr(chat, "full_name", "") or "",
            username=chat.username or "",
        )
        rt.db.seen_member(
            user_id=user.id,
            chat_id=chat.id,
            name=sender,
            username=user.username or "",
        )
    except Exception as exc:  # a bookkeeping failure must never cost a reply
        log.warning("could not record activity for chat %s: %s", chat.id, exc)


async def _announce_failure(rt: Runtime, state: ChatState, message, result: ChatResult) -> None:
    """Say what went wrong, rarely enough that an outage cannot flood the chat."""
    now = time.monotonic()
    if result.error_kind == "throttled":
        # Expected on the free tier and it clears by itself within a minute.
        # Apologising every time would be noisier than the silence.
        return
    if result.error_kind == "payment":
        # Nothing the chat can do about it, and it will not clear on its own.
        if now - state.budget_notice_at > 3600:
            state.budget_notice_at = now
            await send_reply(message, rt.strings("no_credit"), rt)
        return
    if now - state.error_notice_at > ERROR_NOTICE_INTERVAL:
        state.error_notice_at = now
        await send_reply(message, rt.strings("error_reply"), rt)


def _recent_heard(state: ChatState, count: int = RECENT_HEARD) -> list[str]:
    """The last few things people said here, newest first.

    An echo reached further back than the newest message: each turn the bot
    prepended whatever had just arrived to the reply it gave last time, until it
    was handing back three members' sentences in a row.
    """
    said: list[str] = []
    for turn in reversed(state.history):
        if turn.get("role") == "assistant":
            continue
        content = turn.get("content")
        if isinstance(content, str):
            said.append(content)
        if len(said) >= count:
            break
    return said


def _recent_replies(state: ChatState, count: int = RECENT_REPLIES) -> list[str]:
    """The bot's own last few replies here, newest first.

    One was not enough. A tic - the same opening word, or a sentence it already
    used - shows up across a handful of turns and is invisible in a comparison
    against the single reply before.
    """
    said: list[str] = []
    for turn in reversed(state.history):
        if turn.get("role") != "assistant":
            continue
        said.append(str(turn.get("content") or ""))
        if len(said) >= count:
            break
    return said


def _can_read_media(rt: Runtime, bundle: media_mod.MediaBundle) -> bool:
    """Audio needs a paid model; images (including sampled GIF frames) may not."""
    if not rt.settings.free_mode:
        return True
    wants_audio = any(part.get("type") == "input_audio" for part in bundle.parts)
    if wants_audio:
        return False
    return rt.llm.supports_free_vision()


async def _summarize(rt: Runtime, state: ChatState) -> None:
    usage: Usage = await update_notes(rt.llm, rt.settings, state)
    # Notes and the learned style are only in memory until this says so.
    rt.store.mark_dirty()
    rt.record(mode="summary", model=rt.settings.model_summary, usage=usage, chat_id=state.chat_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error", exc_info=context.error)


__all__ = [
    "Allowance",
    "ReplyTarget",
    "build_messages",
    "handle_message",
    "model_params",
    "on_error",
    "reply_target",
]
