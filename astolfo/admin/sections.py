"""What each panel screen shows and what its buttons do."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .. import branding, master, settings_store
from ..config import ConfigError
from .guard import audit
from .ui import ago, back_row, button, confirm_rows, keyboard, trim, yes_no

log = logging.getLogger(__name__)

# Settings worth a button. Everything else is reachable by name through "edit".
COMMON = (
    "free_mode",
    "providers",
    "model_fast",
    "model_think",
    "model_media",
    "group_reply_chance",
    "reply_cooldown",
    "web_search",
    "summaries",
    "daily_budget_usd",
    "locale",
)
TOGGLES = ("free_mode", "web_search", "summaries", "router_llm", "response_cache", "donate_enabled")


@dataclass
class View:
    """One screen of the panel."""

    text: str
    markup: InlineKeyboardMarkup | None = None
    # When set, the next message the owner sends is read as the answer to this.
    prompt: tuple[str, str] | None = None
    alert: str = ""
    document: str = ""
    extras: dict = field(default_factory=dict)


# -- home -----------------------------------------------------------------
def home(ctx) -> View:
    rt = ctx.rt
    counts = rt.db.counts()
    services = ", ".join(p.name for p in rt.llm.providers) or "none"
    spent = rt.budget.today_cost()

    text = (
        "🛠 Astolfo control panel\n\n"
        f"models: {'free' if rt.settings.free_mode else 'paid'} via {services}\n"
        f"groups: {counts['chats']}   people: {counts['users']}\n"
        f"spent today: ${spent:.4f}\n"
        f"owner: {master.describe(rt)}\n\n"
        f"{branding.credit(rt.strings.locale)}"
    )
    return View(
        text,
        keyboard(
            [button("🔌 services", "svc"), button("⚙️ settings", "cfg")],
            [button("💬 groups", "chats"), button("👤 people", "ppl")],
            [button("🖥 server", "srv"), button("🗄 data", "data")],
        ),
    )


# -- settings -------------------------------------------------------------
def config(ctx) -> View:
    rt = ctx.rt
    stored = rt.db.overrides()
    lines = ["⚙️ Settings\n", "★ marks a value changed from here.\n"]
    rows: list[list[InlineKeyboardButton]] = []

    for name in COMMON:
        value = getattr(rt.settings, name)
        shown = ", ".join(str(v) for v in value) if isinstance(value, list) else value
        star = "★" if name in stored else " "
        lines.append(f"{star} {name}: {shown}")
        if name in TOGGLES:
            rows.append([button(f"{name}: {yes_no(value)} → flip", "cfg", "flip", name)])
        else:
            rows.append([button(f"edit {name}", "cfg", "edit", name)])

    rows.append([button("✏️ another setting by name", "cfg", "byname")])
    if stored:
        rows.append([button("↩️ reset one to the .env value", "cfg", "reset")])
    return View("\n".join(lines), keyboard(*rows, back_row()))


def config_prompt(ctx, name: str) -> View:
    if name == "*":
        return View(
            "Send it as `name value`, for example:\n"
            "free_mode 1\nmodel_fast google/gemini-2.5-flash\nproviders openrouter,google",
            keyboard(back_row("cfg")),
            prompt=("setting", "*"),
        )
    if name == "reset":
        stored = ", ".join(ctx.rt.db.overrides()) or "nothing stored"
        return View(
            f"Send the name of the setting to reset.\nStored: {stored}",
            keyboard(back_row("cfg")),
            prompt=("reset", "*"),
        )
    current = getattr(ctx.rt.settings, name, "")
    shown = ", ".join(str(v) for v in current) if isinstance(current, list) else current
    return View(
        f"Send the new value for {name}.\nNow: {shown}\n\nSend /cancel to stop.",
        keyboard(back_row("cfg")),
        prompt=("setting", name),
    )


def config_flip(ctx, name: str) -> View:
    rt = ctx.rt
    new_value = "0" if getattr(rt.settings, name, False) else "1"
    try:
        settings_store.set_override(rt.db, name, new_value, by=ctx.user.id)
    except ConfigError as exc:
        view = config(ctx)
        view.alert = str(exc)
        return view
    view = config(ctx)
    view.alert = f"{name} is now {yes_no(new_value == '1')}"
    view.extras["reload"] = True
    return view


# -- groups ---------------------------------------------------------------
def chats(ctx) -> View:
    rows_data = ctx.rt.db.list_chats(limit=10)
    if not rows_data:
        return View("💬 No groups yet.", keyboard(back_row()))

    lines = ["💬 Groups the bot is in\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for row in rows_data:
        title = trim(row["title"] or str(row["chat_id"]))
        lines.append(
            f"• {title} — {row['people']} people, {row['messages']} messages, "
            f"{ago(row['last_seen'])}"
        )
        rows.append([button(title, "chat", row["chat_id"])])
    return View("\n".join(lines), keyboard(*rows, back_row()))


def chat_detail(ctx, chat_id: int) -> View:
    rt = ctx.rt
    row = rt.db.chat(chat_id)
    if row is None:
        return View("that group is not in the database", keyboard(back_row("chats")))

    state = rt.store.get(chat_id)
    chance = state.reply_chance
    if chance is None:
        chance = rt.settings.group_reply_chance
    text = (
        f"💬 {row['title'] or chat_id}\n\n"
        f"id: {row['chat_id']}\ntype: {row['type'] or '?'}\n"
        f"messages seen: {row['messages']}\nreplies sent: {row['replies']}\n"
        f"last activity: {ago(row['last_seen'])}\n"
        f"muted: {yes_no(row['muted'])}\n"
        f"joins in: {round(chance * 100)}% of the time\n"
        f"mode: {state.forced_mode or 'auto'}\n"
        f"notes: {trim(row['notes'] or '-', 60)}"
    )
    unmute = bool(row["muted"])
    return View(
        text,
        keyboard(
            [
                button("unmute" if unmute else "mute", "chat", chat_id, "mute", 0 if unmute else 1),
                button("👥 people", "chat", chat_id, "people"),
            ],
            [button("🚪 leave this group", "chat", chat_id, "leave")],
            back_row("chats"),
        ),
    )


def chat_mute(ctx, chat_id: int, muted: bool) -> View:
    rt = ctx.rt
    rt.store.get(chat_id).muted = muted
    rt.store.mark_dirty()
    rt.db.save_chat_state(chat_id, muted=1 if muted else 0)
    audit(rt, ctx.user, "mute" if muted else "unmute", str(chat_id))
    view = chat_detail(ctx, chat_id)
    view.alert = "muted" if muted else "unmuted"
    return view


async def chat_leave(ctx, chat_id: int, confirmed: bool) -> View:
    if not confirmed:
        return View(
            "Leave this group?\nThe bot has to be added again by an admin to come back.",
            keyboard(*confirm_rows("leave", "chat", chat_id, "leave!")),
        )

    rt = ctx.rt
    try:
        await ctx.bot.leave_chat(chat_id)
    except Exception as exc:
        view = chat_detail(ctx, chat_id)
        view.alert = f"could not leave: {exc}"
        return view

    rt.db.left_chat(chat_id)
    audit(rt, ctx.user, "leave_chat", str(chat_id))
    view = chats(ctx)
    view.alert = "left the group"
    return view


# -- people ---------------------------------------------------------------
def people(ctx, chat_id: int | None = None) -> View:
    rt = ctx.rt
    if chat_id is None:
        recent = rt.db.query("SELECT * FROM users ORDER BY last_seen DESC LIMIT 10")
        header = "👤 Most recently active"
    else:
        recent = rt.db.members(chat_id, limit=10)
        row = rt.db.chat(chat_id)
        header = f"👥 In {trim(row['title'] if row else str(chat_id))}"

    lines = [header + "\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for row in recent:
        flag = "🚫 " if row["blocked"] else ""
        lines.append(
            f"{flag}{trim(row['name'])} — {row['messages']} messages, {ago(row['last_seen'])}"
        )
        rows.append([button(f"{flag}{trim(row['name'], 20)}", "ppl", row["user_id"])])

    rows.append([button("🔍 find someone", "ppl", "find")])
    return View("\n".join(lines), keyboard(*rows, back_row()))


def person_prompt(ctx) -> View:
    return View(
        "Send a name, a @username or a numeric id.",
        keyboard(back_row("ppl")),
        prompt=("person", "*"),
    )


def person_detail(ctx, user_id: int) -> View:
    rt = ctx.rt
    row = rt.db.user(user_id)
    if row is None:
        return View("nobody with that id has spoken to the bot", keyboard(back_row("ppl")))

    seen_in = rt.db.query(
        """
        SELECT c.title, m.messages FROM members m
        LEFT JOIN chats c ON c.chat_id = m.chat_id
        WHERE m.user_id = ? ORDER BY m.messages DESC LIMIT 5
        """,
        (user_id,),
    )
    where = (
        "\n".join(f"• {trim(r['title'] or '?')}: {r['messages']}" for r in seen_in) or "• nowhere"
    )
    text = (
        f"👤 {row['name']}\n\n"
        f"id: {row['user_id']}\n"
        f"username: @{row['username'] or '-'}\n"
        f"messages: {row['messages']}\n"
        f"first seen: {ago(row['first_seen'])}\nlast seen: {ago(row['last_seen'])}\n"
        f"blocked: {yes_no(row['blocked'])}\n\nseen in:\n{where}"
    )
    blocked = bool(row["blocked"])
    return View(
        text,
        keyboard(
            [
                button(
                    "unblock" if blocked else "🚫 block",
                    "ppl", user_id, "block", 0 if blocked else 1,
                )
            ],
            back_row("ppl"),
        ),
    )


def person_block(ctx, user_id: int, blocked: bool) -> View:
    ctx.rt.set_blocked(user_id, blocked)
    audit(ctx.rt, ctx.user, "block" if blocked else "unblock", str(user_id))
    view = person_detail(ctx, user_id)
    view.alert = "blocked — the bot ignores them now" if blocked else "unblocked"
    return view


# -- data -----------------------------------------------------------------
def data(ctx) -> View:
    counts = ctx.rt.db.counts()
    lines = ["🗄 Database\n"] + [f"{name}: {count}" for name, count in counts.items()]
    lines.append(f"\nfile: {ctx.rt.db.path}")
    return View(
        "\n".join(lines),
        keyboard(
            [button("📜 recent actions", "data", "audit")],
            [button("⬇️ send me a backup", "data", "backup")],
            [button("🧹 compact", "data", "vacuum")],
            back_row(),
        ),
    )


def audit_trail(ctx) -> View:
    rows = ctx.rt.db.audit_trail(limit=15)
    if not rows:
        return View("📜 nothing recorded yet", keyboard(back_row("data")))
    lines = ["📜 Recent actions\n"]
    for row in rows:
        detail = f" — {trim(row['detail'], 40)}" if row["detail"] else ""
        lines.append(f"{ago(row['at'])}: {row['action']}{detail}")
    return View("\n".join(lines), keyboard(back_row("data")))


def vacuum(ctx) -> View:
    ctx.rt.db.vacuum()
    audit(ctx.rt, ctx.user, "vacuum")
    view = data(ctx)
    view.alert = "database compacted"
    return view


def backup(ctx) -> View:
    """Hand the owner the database file itself, so a backup is one press."""
    ctx.rt.save(force=True)
    audit(ctx.rt, ctx.user, "backup")
    view = data(ctx)
    view.document = ctx.rt.db.path
    view.alert = "sending the database"
    return view
