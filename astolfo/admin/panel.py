"""The /panel command, its buttons, and the typed answers they ask for."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from .. import runtime, server_ops, settings_store
from ..config import ConfigError
from . import models as models_section
from . import sections, server
from . import services as services_section
from .guard import allowed
from .sections import View
from .ui import PREFIX

log = logging.getLogger(__name__)

PATTERN = rf"^{PREFIX}:"
PROMPT_KEY = "panel_prompt"


@dataclass
class Ctx:
    """Everything a section needs, without reaching back into Telegram."""

    rt: object
    user: object
    bot: object


async def open_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    rt = runtime.get(context)
    context.user_data.pop(PROMPT_KEY, None)
    ctx = Ctx(rt=rt, user=update.effective_user, bot=context.bot)
    await _send(sections.home(ctx), update.effective_message, context)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    # Checked again here on purpose: a panel message can be forwarded, and its
    # buttons keep working for whoever presses them unless we say otherwise.
    if not allowed(update, context):
        await query.answer("not for you", show_alert=False)
        return

    rt = runtime.get(context)
    ctx = Ctx(rt=rt, user=update.effective_user, bot=context.bot)
    parts = (query.data or "").split(":")[1:]
    try:
        view = await _route(ctx, parts)
    except Exception as exc:
        log.exception("panel action failed: %s", query.data)
        view = View(f"that did not work: {exc}", sections.home(ctx).markup)

    await query.answer(view.alert[:200] if view.alert else None, show_alert=bool(view.alert))
    if view.extras.get("reload"):
        await rt.reconfigure(settings_store.reload(rt.db))
    context.user_data[PROMPT_KEY] = view.prompt if view.prompt else None
    await _edit(view, query, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read a typed answer, but only when the panel is waiting for one."""
    pending = context.user_data.get(PROMPT_KEY)
    if not pending or not allowed(update, context):
        return  # anything else is an ordinary message for the chat pipeline

    message = update.effective_message
    text = (message.text or "").strip()
    rt = runtime.get(context)
    ctx = Ctx(rt=rt, user=update.effective_user, bot=context.bot)
    context.user_data[PROMPT_KEY] = None

    if text.lower() in {"/cancel", "cancel"}:
        await _send(sections.home(ctx), message, context)
        raise ApplicationHandlerStop

    kind, target = pending
    view = await _answer(ctx, kind, target, text, message)
    if view.extras.get("reload"):
        await rt.reconfigure(settings_store.reload(rt.db))
    context.user_data[PROMPT_KEY] = view.prompt if view.prompt else None
    await _send(view, message, context)
    raise ApplicationHandlerStop


# -- routing --------------------------------------------------------------
async def _route(ctx: Ctx, parts: list[str]) -> View:
    head = parts[0] if parts else "home"
    rest = parts[1:]

    if head == "home":
        return sections.home(ctx)

    if head in ("svc", "keys"):  # "keys" keeps older panel messages working
        return await _services(ctx, rest)

    if head == "mdl":
        return await _models(ctx, rest)

    if head == "cfg":
        action = rest[0] if rest else ""
        name = rest[1] if len(rest) > 1 else ""
        if action == "edit":
            return sections.config_prompt(ctx, name)
        if action == "byname":
            return sections.config_prompt(ctx, "*")
        if action == "reset":
            return sections.config_prompt(ctx, "reset")
        if action == "flip":
            return sections.config_flip(ctx, name)
        return sections.config(ctx)

    if head == "chats":
        action = rest[0] if rest else ""
        if action == "all" and len(rest) > 1:
            return sections.chats_all_mode(ctx, rest[1])
        if action == "alllimit":
            return sections.chats_all_limit_prompt(ctx)
        return sections.chats(ctx)

    if head == "chat":
        chat_id = int(rest[0])
        action = rest[1] if len(rest) > 1 else ""
        if action == "mute":
            return sections.chat_mute(ctx, chat_id, muted=rest[2] == "1")
        if action == "people":
            return sections.people(ctx, chat_id)
        if action == "mode" and len(rest) > 2:
            return sections.chat_mode(ctx, chat_id, rest[2])
        if action == "limit":
            return sections.chat_limit_prompt(ctx, chat_id)
        if action in ("leave", "leave!"):
            return await sections.chat_leave(ctx, chat_id, confirmed=action.endswith("!"))
        return sections.chat_detail(ctx, chat_id)

    if head == "ppl":
        if not rest:
            return sections.people(ctx)
        if rest[0] == "find":
            return sections.person_prompt(ctx)
        user_id = int(rest[0])
        if len(rest) > 2 and rest[1] == "block":
            return sections.person_block(ctx, user_id, blocked=rest[2] == "1")
        if len(rest) > 1 and rest[1] == "limit":
            return sections.person_limit_prompt(ctx, user_id)
        return sections.person_detail(ctx, user_id)

    if head == "srv":
        action = rest[0] if rest else ""
        if action == "check":
            return server.check(ctx)
        if action == "log":
            return server.log(ctx)
        if action.rstrip("!") in server_ops.ACTIONS:
            return server.job(ctx, action.rstrip("!"), confirmed=action.endswith("!"))
        return server.overview(ctx)

    if head == "data":
        action = rest[0] if rest else ""
        if action == "audit":
            return sections.audit_trail(ctx)
        if action == "vacuum":
            return sections.vacuum(ctx)
        if action == "backup":
            return sections.backup(ctx)
        return sections.data(ctx)

    return sections.home(ctx)


async def _models(ctx: Ctx, rest: list[str]) -> View:
    head = rest[0] if rest else ""

    if head == "sync":
        return await models_section.sync(ctx)
    if head == "u":
        return models_section.usage(ctx)
    if head == "find" and len(rest) > 2:
        return models_section.ask_search(ctx, rest[1], rest[2] != "0")
    if head == "r" and len(rest) > 3:
        return models_section.pick(ctx, rest[1], int(rest[2]), rest[3] != "0")
    if head == "set" and len(rest) > 3:
        return models_section.choose(ctx, rest[1], int(rest[3]), rest[2] != "0")
    return models_section.overview(ctx)


async def _services(ctx: Ctx, rest: list[str]) -> View:
    head = rest[0] if rest else ""

    if head == "testall":
        return await services_section.test_all(ctx)
    if head == "new":
        return services_section.ask_new_service(ctx)
    if head == "pin" and len(rest) > 1:
        return services_section.pin(ctx, rest[1])

    if head == "k" and len(rest) > 1:
        credential_id = int(rest[1])
        action = rest[2] if len(rest) > 2 else ""
        if action in ("on", "off"):
            return services_section.key_enabled(ctx, credential_id, enabled=action == "on")
        if action in ("rm", "rm!"):
            return services_section.key_remove(ctx, credential_id, confirmed=action.endswith("!"))
        return services_section.key_detail(ctx, credential_id)

    if head == "s" and len(rest) > 1:
        name = rest[1]
        action = rest[2] if len(rest) > 2 else ""
        if action == "test":
            return await services_section.test(ctx, name)
        if action in ("on", "off"):
            return services_section.set_enabled(ctx, name, enabled=action == "on")
        if action in ("up", "down"):
            return services_section.move(ctx, name, -1 if action == "up" else 1)
        if action == "wake":
            return services_section.wake(ctx, name)
        if action == "addkey":
            return services_section.ask_key(ctx, name)
        if action in ("models", "url"):
            return services_section.ask_field(ctx, name, action)
        if action in ("del", "del!"):
            return services_section.delete(ctx, name, confirmed=action.endswith("!"))
        return services_section.detail(ctx, name)

    return services_section.overview(ctx)


async def _answer(ctx: Ctx, kind: str, target: str, text: str, message) -> View:
    if kind == "svckey":
        await _forget(message)
        return services_section.take_key(ctx, target, text)

    if kind == "svcnew":
        return services_section.take_new_service(ctx, text)

    if kind == "mdlfind":
        return models_section.take_search(ctx, target, text)

    if kind in ("svcmodels", "svcurl"):
        return services_section.take_field(
            ctx, target, "models" if kind == "svcmodels" else "url", text
        )

    if kind == "setting":
        name, _, raw = text.partition(" ") if target == "*" else (target, "", text)
        try:
            settings_store.set_override(ctx.rt.db, name.strip(), raw.strip(), by=ctx.user.id)
        except ConfigError as exc:
            view = sections.config(ctx)
            view.alert = str(exc)
            view.text = f"❌ {exc}\n\n{view.text}"
            return view
        view = sections.config(ctx)
        view.text = f"✅ {name.strip()} updated\n\n{view.text}"
        view.extras["reload"] = True
        return view

    if kind == "reset":
        settings_store.clear_override(ctx.rt.db, text.strip(), by=ctx.user.id)
        view = sections.config(ctx)
        view.text = f"↩️ {text.strip()} follows the .env value again\n\n{view.text}"
        view.extras["reload"] = True
        return view

    if kind == "chatlimit":
        return sections.chat_limit(ctx, int(target), text)

    if kind == "userlimit":
        return sections.person_limit(ctx, int(target), text)

    if kind == "alllimit":
        return sections.chats_all_limit(ctx, text)

    if kind == "person":
        found = ctx.rt.db.find_people(text.lstrip("@"))
        if len(found) == 1:
            return sections.person_detail(ctx, int(found[0]["user_id"]))
        if not found:
            view = sections.people(ctx)
            view.text = f"nobody matching {text}\n\n{view.text}"
            return view
        view = sections.people(ctx)
        view.text = "\n".join(
            [f"matches for {text}:"] + [f"• {r['name']} — id {r['user_id']}" for r in found[:10]]
        )
        return view

    return sections.home(ctx)


async def _forget(message) -> None:
    """Get a key out of the chat history the moment it has been read."""
    try:
        await message.delete()
    except Exception as exc:
        log.warning("could not delete the message carrying a key: %s", exc)


# -- rendering ------------------------------------------------------------
async def _send(view: View, message, context) -> None:
    await message.reply_text(view.text, reply_markup=view.markup, disable_web_page_preview=True)
    await _send_document(view, message, context)


async def _edit(view: View, query, context) -> None:
    try:
        await query.edit_message_text(
            view.text, reply_markup=view.markup, disable_web_page_preview=True
        )
    except Exception as exc:
        # "message is not modified" is the common one and means the screen is fine.
        log.debug("panel edit skipped: %s", exc)
    await _send_document(view, query.message, context)


async def _send_document(view: View, message, context) -> None:
    if not view.document:
        return
    try:
        with open(view.document, "rb") as fh:
            await context.bot.send_document(
                chat_id=message.chat_id, document=fh, filename="astolfo.db"
            )
    except Exception as exc:
        log.warning("could not send the database: %s", exc)
        await message.reply_text(f"could not send the file: {exc}")
