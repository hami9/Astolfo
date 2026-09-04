"""What each panel screen shows and what its buttons do."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .. import branding, master, participation, settings_store
from ..config import ConfigError
from .guard import audit
from .ui import ago, back_row, button, confirm_rows, keyboard, trim, yes_no

log = logging.getLogger(__name__)

# Settings worth a button, in the order somebody actually reaches for them.
# Models and services are not here on purpose: they have screens of their own
# that pick from the live catalog, which beats typing a model id by hand.
COMMON = (
    "free_mode",
    "group_reply_chance",
    "reply_cooldown",
    "max_tokens_fast",
    "adaptive_length",
    "interest_scoring",
    "attention_hold",
    "heavy_lifting",
    "read_admins",
    "web_search",
    "summaries",
    "daily_budget_usd",
    "locale",
)
TOGGLES = (
    "free_mode",
    "web_search",
    "summaries",
    "router_llm",
    "response_cache",
    "donate_enabled",
    "adaptive_length",
    "interest_scoring",
    "heavy_lifting",
    "read_admins",
)


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
            [button("🔌 services", "svc"), button("🧠 models", "mdl")],
            [button("⚙️ settings", "cfg"), button("💬 groups", "chats")],
            [button("👤 people", "ppl"), button("🖥 server", "srv")],
            [button("🗄 data", "data")],
        ),
    )


# -- settings -------------------------------------------------------------
def config(ctx) -> View:
    rt = ctx.rt
    stored = rt.db.overrides()
    header = "★ marks a value changed from here. Models and services have screens of their own."
    lines = ["⚙️ Settings\n", f"{header}\n"]
    # Thirteen full-width buttons is a wall you have to scroll past to reach the
    # back button. Two to a row fits the screen and reads as a grid.
    pairs: list[InlineKeyboardButton] = []

    for name in COMMON:
        value = getattr(rt.settings, name, None)
        shown = ", ".join(str(v) for v in value) if isinstance(value, list) else value
        star = "★" if name in stored else " "
        lines.append(f"{star} {name}: {yes_no(value) if name in TOGGLES else shown}")
        pairs.append(
            button(f"{_short(name)}: {yes_no(value)}", "cfg", "flip", name)
            if name in TOGGLES
            else button(f"✏️ {_short(name)}", "cfg", "edit", name)
        )

    rows = [pairs[i : i + 2] for i in range(0, len(pairs), 2)]
    rows.append([button("🧠 models", "mdl"), button("🔌 services", "svc")])
    rows.append([button("✏️ another setting by name", "cfg", "byname")])
    if stored:
        rows.append([button("↩️ reset one to the .env value", "cfg", "reset")])
    return View("\n".join(lines), keyboard(*rows, back_row()))


def _short(name: str) -> str:
    """Two of these share a row, so the label has to fit beside another one."""
    return {
        "group_reply_chance": "join chance",
        "reply_cooldown": "cooldown",
        "max_tokens_fast": "reply length",
        "adaptive_length": "auto length",
        "interest_scoring": "join on merit",
        "attention_hold": "focus hold",
        "heavy_lifting": "does homework",
        "read_admins": "reads admins",
        "daily_budget_usd": "daily budget",
        "free_mode": "free mode",
        "web_search": "web search",
    }.get(name, name.replace("_", " "))


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
KINDS = {"private": "private chat", "group": "group", "supergroup": "group", "channel": "channel"}


def chat_name(row) -> str:
    """Something recognisable, in the order it is worth showing.

    A private chat has no title, so before this it showed as a bare numeric id
    on every screen - which is the one thing that tells you nothing about which
    conversation you are about to mute or leave.
    """
    available = set(row.keys())
    for key in ("title", "person", "username"):
        value = str(row[key] or "").strip() if key in available else ""
        if value:
            return f"@{value}" if key == "username" else value
    return f"chat {row['chat_id']}"


def chat_marks(row, state=None) -> str:
    marks = "🔇" if row["muted"] else ""
    if state is not None and state.off:
        marks += "⏻"
    return f" {marks}" if marks else ""


def chats(ctx) -> View:
    rows_data = ctx.rt.db.list_chats(limit=10)
    if not rows_data:
        return View("💬 No groups yet.", keyboard(back_row()))

    lines = ["💬 Groups the bot is in\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for row in rows_data:
        name = chat_name(row)
        state = ctx.rt.store.get(int(row["chat_id"]))
        kind = KINDS.get(str(row["type"] or ""), "chat")
        handle = f" @{row['username']}" if row["username"] else ""
        lines.append(
            f"• {trim(name, 32)}{handle}{chat_marks(row, state)}\n"
            f"   {kind} · {row['people']} people · {row['messages']} messages"
            f" · {ago(row['last_seen'])}"
        )
        rows.append([button(f"{trim(name, 26)}{chat_marks(row, state)}", "chat", row["chat_id"])])

    lines.append(f"\nglobal mode: {ctx.rt.settings.reply_mode}")
    return View(
        "\n".join(lines),
        keyboard(
            *rows,
            [
                button("all → 🗣 manual", "chats", "all", participation.MANUAL),
                button("all → 💬 auto", "chats", "all", participation.AUTO),
                button("all → 🧠 smart", "chats", "all", participation.SMART),
            ],
            [
                button("↩️ all follow global", "chats", "all", "-"),
                button("🔢 limit for all", "chats", "alllimit"),
            ],
            back_row(),
        ),
    )


def chats_all_mode(ctx, mode: str) -> View:
    """Set every group at once, for when the answer is the same everywhere."""
    chosen = "" if mode == "-" else participation.normalize(mode)
    rt = ctx.rt
    touched = rt.db.set_every_chat(mode=chosen)
    for state in rt.store.all_states():
        state.mode = chosen
    audit(rt, ctx.user, "chats_mode", f"{touched} groups → {chosen or 'global'}")

    view = chats(ctx)
    view.alert = f"{touched} group(s) now answer: {chosen or 'as the global mode says'}"
    return view


def chats_all_limit_prompt(ctx) -> View:
    return View(
        "How many model calls a day may each group use?\n\n"
        "Send a number, or 0 to hand them all back to the global limit.",
        keyboard(back_row("chats")),
        prompt=("alllimit", "*"),
    )


def chats_all_limit(ctx, raw: str) -> View:
    try:
        limit = max(0, int(raw.strip()))
    except ValueError:
        view = chats(ctx)
        view.text = f"❌ {raw.strip()} is not a number\n\n{view.text}"
        return view

    rt = ctx.rt
    touched = rt.db.set_every_chat(daily_limit=limit)
    for state in rt.store.all_states():
        state.daily_limit = limit
    audit(rt, ctx.user, "chats_limit", f"{touched} groups → {limit}")

    view = chats(ctx)
    view.text = f"✅ {touched} group(s): {limit or 'no limit of their own'}\n\n{view.text}"
    return view


def chat_detail(ctx, chat_id: int) -> View:
    rt = ctx.rt
    row = rt.db.chat(chat_id)
    if row is None:
        return View("that group is not in the database", keyboard(back_row("chats")))

    state = rt.store.get(chat_id)
    chance = state.reply_chance
    if chance is None:
        chance = rt.settings.group_reply_chance
    mode, why = participation.effective(rt, state)
    if participation.mode_for(rt, state) == participation.SMART:
        mode = f"smart → {mode}"
    limit = state.daily_limit or rt.settings.chat_daily_call_limit or "none"
    handle = f"\nusername: @{row['username']}" if row["username"] else ""
    text = (
        f"💬 {chat_name(row)}\n\n"
        f"id: {row['chat_id']}\ntype: {KINDS.get(str(row['type'] or ''), '?')}{handle}\n"
        f"people: {row['people']}\n"
        f"messages seen: {row['messages']}\nreplies sent: {row['replies']}\n"
        f"last activity: {ago(row['last_seen'])}\n"
        f"muted: {yes_no(row['muted'])}\n"
        f"switched off: {yes_no(state.off)}\n"
        f"joins in: {round(chance * 100)}% of the time\n"
        f"answers: {mode} ({why})\n"
        f"daily limit: {limit}\n"
        f"used today: {rt.budget.chat_calls_today(chat_id)} calls\n"
        f"mode: {state.forced_mode or 'auto'}\n"
        f"notes: {trim(row['notes'] or '-', 60)}\n"
        f"learned style: {trim(state.style.summary(), 120)}"
    )
    unmute = bool(row["muted"])
    if state.off:
        text += (
            "\n\n⏻ This group is switched off. Nothing said here is read, stored, "
            "counted or answered, and commands do not work either — so it is turned "
            "back on from this screen."
        )
    return View(
        text,
        keyboard(
            [
                button(
                    "⏻ turn back on" if state.off else "⏻ switch off entirely",
                    "chat", chat_id, "off", 0 if state.off else 1,
                ),
            ],
            [
                button("unmute" if unmute else "mute", "chat", chat_id, "mute", 0 if unmute else 1),
                button("👥 people", "chat", chat_id, "people"),
            ],
            [
                button("🗣 manual", "chat", chat_id, "mode", participation.MANUAL),
                button("💬 auto", "chat", chat_id, "mode", participation.AUTO),
                button("🧠 smart", "chat", chat_id, "mode", participation.SMART),
            ],
            [
                button("↩️ follow the global mode", "chat", chat_id, "mode", "-"),
                button("🔢 daily limit", "chat", chat_id, "limit"),
            ],
            [button("🧠 forget the learned style", "chat", chat_id, "unlearn")],
            [button("🚪 leave this group", "chat", chat_id, "leave")],
            back_row("chats"),
        ),
    )


def chat_mode(ctx, chat_id: int, mode: str) -> View:
    """Set how talkative the bot is here, or hand it back to the global setting."""
    chosen = "" if mode == "-" else participation.normalize(mode)
    rt = ctx.rt
    rt.store.get(chat_id).mode = chosen
    rt.store.mark_dirty()
    rt.db.save_chat_state(chat_id, mode=chosen)
    audit(rt, ctx.user, "chat_mode", f"{chat_id} {chosen or 'global'}")

    view = chat_detail(ctx, chat_id)
    view.alert = f"answers here: {chosen or 'whatever the global mode says'}"
    return view


def chat_unlearn(ctx, chat_id: int) -> View:
    """Drop what the bot picked up about talking here, and start it over."""
    rt = ctx.rt
    state = rt.store.get(chat_id)
    state.style.forget()
    rt.store.mark_dirty()
    rt.db.save_chat_state(chat_id, style="")
    audit(rt, ctx.user, "chat_unlearn", str(chat_id))

    view = chat_detail(ctx, chat_id)
    view.alert = "forgotten; it starts learning this chat again from here"
    return view


def chat_limit_prompt(ctx, chat_id: int) -> View:
    state = ctx.rt.store.get(chat_id)
    return View(
        f"How many model calls a day may this group use?\n"
        f"Now: {state.daily_limit or 'no limit of its own'}\n\n"
        "Send a number, or 0 to follow the global limit.",
        keyboard(back_row("chat", chat_id)),
        prompt=("chatlimit", str(chat_id)),
    )


def chat_limit(ctx, chat_id: int, raw: str) -> View:
    try:
        limit = max(0, int(raw.strip()))
    except ValueError:
        view = chat_detail(ctx, chat_id)
        view.text = f"❌ {raw.strip()} is not a number\n\n{view.text}"
        return view

    rt = ctx.rt
    rt.store.get(chat_id).daily_limit = limit
    rt.store.mark_dirty()
    rt.db.save_chat_state(chat_id, daily_limit=limit)
    audit(rt, ctx.user, "chat_limit", f"{chat_id} {limit}")

    view = chat_detail(ctx, chat_id)
    view.text = f"✅ daily limit: {limit or 'follows the global one'}\n\n{view.text}"
    return view


def chat_off(ctx, chat_id: int, *, off: bool) -> View:
    """Switch a group off entirely, or bring it back.

    Muted stops the bot talking. This stops it listening: nothing said there is
    read, stored, counted or answered, and its commands go unanswered too, which
    is why the way back is this screen rather than a command in the group.
    """
    ctx.rt.set_chat_off(chat_id, off)
    audit(ctx.rt, ctx.user, "chat_off" if off else "chat_on", str(chat_id))
    view = chat_detail(ctx, chat_id)
    view.alert = "switched off - it reads nothing there now" if off else "back on"
    return view


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
        header = f"👥 In {trim(chat_name(row) if row else f'chat {chat_id}')}"

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

    # The same fallbacks the groups list uses. Selecting only the title is why a
    # private chat, which has none, showed up here as a bare "?".
    seen_in = rt.db.query(
        """
        SELECT m.chat_id, m.messages, c.title, c.username,
               (SELECT u.name FROM users u WHERE u.user_id = m.chat_id) AS person
        FROM members m LEFT JOIN chats c ON c.chat_id = m.chat_id
        WHERE m.user_id = ? ORDER BY m.messages DESC LIMIT 5
        """,
        (user_id,),
    )
    where = (
        "\n".join(f"• {trim(chat_name(r), 30)}: {r['messages']}" for r in seen_in) or "• nowhere"
    )
    limit = rt.limit_for(user_id) or rt.settings.user_daily_call_limit or "none"
    text = (
        f"👤 {row['name']}\n\n"
        f"id: {row['user_id']}\n"
        f"username: @{row['username'] or '-'}\n"
        f"messages: {row['messages']}\n"
        f"first seen: {ago(row['first_seen'])}\nlast seen: {ago(row['last_seen'])}\n"
        f"blocked: {yes_no(row['blocked'])}\n"
        f"daily limit: {limit}\n"
        f"used today: {rt.budget.user_calls_today(user_id)} calls\n"
        f"\nseen in:\n{where}"
    )
    blocked = bool(row["blocked"])
    return View(
        text,
        keyboard(
            [
                button(
                    "unblock" if blocked else "🚫 block",
                    "ppl", user_id, "block", 0 if blocked else 1,
                ),
                button("🔢 daily limit", "ppl", user_id, "limit"),
            ],
            back_row("ppl"),
        ),
    )


def person_limit_prompt(ctx, user_id: int) -> View:
    current = ctx.rt.limit_for(user_id)
    return View(
        f"How many model calls a day may this person use?\n"
        f"Now: {current or 'no limit of their own'}\n\n"
        "Send a number, or 0 to follow the global limit.",
        keyboard(back_row("ppl", user_id)),
        prompt=("userlimit", str(user_id)),
    )


def person_limit(ctx, user_id: int, raw: str) -> View:
    try:
        limit = max(0, int(raw.strip()))
    except ValueError:
        view = person_detail(ctx, user_id)
        view.text = f"❌ {raw.strip()} is not a number\n\n{view.text}"
        return view

    ctx.rt.set_user_limit(user_id, limit)
    audit(ctx.rt, ctx.user, "user_limit", f"{user_id} {limit}")
    view = person_detail(ctx, user_id)
    view.text = f"✅ daily limit: {limit or 'follows the global one'}\n\n{view.text}"
    return view


def person_block(ctx, user_id: int, blocked: bool) -> View:
    ctx.rt.set_blocked(user_id, blocked)
    audit(ctx.rt, ctx.user, "block" if blocked else "unblock", str(user_id))
    view = person_detail(ctx, user_id)
    view.alert = "blocked — the bot ignores them now" if blocked else "unblocked"
    return view


# -- data -----------------------------------------------------------------
def data(ctx) -> View:
    rt = ctx.rt
    counts = rt.db.counts()
    lines = ["🗄 Database\n"] + [f"{name}: {count}" for name, count in counts.items()]
    lines.append(f"\non disk: {_megabytes(rt.db.size_bytes())}")
    keep = rt.settings.retain_days
    lines.append(
        f"kept for: {keep} days, cleaned daily"
        if keep > 0
        else "kept for: forever (RETAIN_DAYS=0)"
    )
    lines.append(f"file: {rt.db.path}")
    return View(
        "\n".join(lines),
        keyboard(
            [button("📜 recent actions", "data", "audit")],
            [button("🧽 clean up now", "data", "prune")],
            [button("⬇️ send me a backup", "data", "backup")],
            [button("🧹 compact", "data", "vacuum")],
            back_row(),
        ),
    )


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{size // 1024} KB"


def prune(ctx) -> View:
    """Drop what is past the retention window, and say what went."""
    before = ctx.rt.db.size_bytes()
    removed = ctx.rt.db.prune(ctx.rt.settings.retain_days)
    audit(ctx.rt, ctx.user, "prune", ", ".join(f"{n} {k}" for k, n in removed.items()))

    view = data(ctx)
    if not removed:
        view.alert = "nothing old enough to remove"
        return view
    freed = max(0, before - ctx.rt.db.size_bytes())
    what = ", ".join(f"{count} {name}" for name, count in removed.items())
    view.alert = f"removed {what}; {_megabytes(freed)} freed"
    return view


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
