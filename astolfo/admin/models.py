"""Choosing which model does what, from the list the service actually offers.

Free models on OpenRouter appear and disappear weekly. Editing `.env` and
restarting for each one is the thing this screen exists to stop: the catalog is
read from the service, shown as a list, and a press writes the setting - which
takes effect on the next message, with no restart.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton

from .. import settings_store
from ..catalog import Model
from .guard import audit
from .sections import View
from .ui import ago, back_row, button, keyboard, trim

log = logging.getLogger(__name__)

PER_PAGE = 6

# The jobs a model can be given, in the order they matter.
ROLES: dict[str, str] = {
    "fast": "model_fast",
    "think": "model_think",
    "search": "model_search",
    "media": "model_media",
    "router": "model_router",
    "summary": "model_summary",
}

WHAT_FOR = {
    "fast": "everyday chatter",
    "think": "reasoning-heavy turns",
    "search": "grounded answers",
    "media": "photos, voice, video",
    "router": "deciding the mode",
    "summary": "folding old turns into notes",
}


def _tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _pool(ctx, free_only: bool, vision: bool = False) -> list[Model]:
    return ctx.rt.llm.models_offered(free_only=free_only, vision=vision)


def _flag(value: object) -> int:
    return 1 if value else 0


# -- the roles ------------------------------------------------------------
def overview(ctx) -> View:
    """What each job is set to now, and what it did today."""
    rt = ctx.rt
    usage = dict(rt.budget.model_usage())
    offered = _pool(ctx, free_only=True)

    lines = ["🧠 Models\n"]
    if offered:
        services = sorted({m.service for m in offered if m.service})
        where = f" across {', '.join(services)}" if services else ""
        lines.append(f"{len(offered)} free chat models offered right now{where}.\n")
    else:
        lines.append("The catalog has not been read yet — press sync.\n")

    rows: list[list[InlineKeyboardButton]] = []
    for role, setting in ROLES.items():
        current = getattr(rt.settings, setting, "") or "-"
        row = usage.get(current)
        did = ""
        if row and row["calls"]:
            did = f"  · {row['calls']} calls, {_tokens(row['prompt'] + row['completion'])} tokens"
        lines.append(f"{role}: {current}{did}")
        rows.append([button(f"{role} → change", "mdl", "r", role, 1, 1)])

    lines.append(f"\nfree mode: {'on' if rt.settings.free_mode else 'off'}")
    if rt.settings.free_mode:
        lines.append("In free mode the pool is used automatically; these are the paid-mode jobs.")

    rows.append([button("📊 token usage", "mdl", "u"), button("🆕 what is new", "mdl", "new")])
    rows.append([button("🔄 sync catalog", "mdl", "sync")])
    rows.append(back_row())
    return View("\n".join(lines), keyboard(*rows))


def pick(ctx, role: str, page: int, free_only: bool) -> View:
    """The list to choose from, a page at a time."""
    if role not in ROLES:
        return overview(ctx)
    # The media job is the only one that has to be able to see.
    models = _pool(ctx, free_only=free_only, vision=role == "media")
    if not models:
        view = overview(ctx)
        view.alert = "no models in the catalog yet; press sync"
        return view

    pages = max(1, (len(models) + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, pages))
    start = (page - 1) * PER_PAGE
    shown = models[start : start + PER_PAGE]
    current = getattr(ctx.rt.settings, ROLES[role], "")

    lines = [
        f"🧠 {role} — {WHAT_FOR[role]}",
        f"now: {current or '-'}\n",
        f"{'free only' if free_only else 'every model'}"
        + (", vision" if role == "media" else "")
        + f" · {len(models)} found · page {page}/{pages}\n",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for offset, model in enumerate(shown):
        here = "✓ " if model.id == current else ""
        lines.append(f"{here}{model.short}  {model.window}  {model.marks}  {model.price}")
        rows.append(
            [
                button(
                    f"{here}{trim(model.short, 30)} · {model.window}",
                    "mdl",
                    "set",
                    role,
                    _flag(free_only),
                    start + offset,
                )
            ]
        )

    paging = []
    if page > 1:
        paging.append(button("‹ prev", "mdl", "r", role, page - 1, _flag(free_only)))
    if page < pages:
        paging.append(button("next ›", "mdl", "r", role, page + 1, _flag(free_only)))
    if paging:
        rows.append(paging)

    rows.append(
        [
            button(
                "show every model" if free_only else "free only",
                "mdl",
                "r",
                role,
                1,
                _flag(not free_only),
            ),
            button("🔎 search", "mdl", "find", role, _flag(free_only)),
        ]
    )
    rows.append(back_row("mdl"))
    return View("\n".join(lines), keyboard(*rows))


def choose(ctx, role: str, index: int, free_only: bool) -> View:
    """Give this job to the model at that position in the list just shown."""
    if role not in ROLES:
        return overview(ctx)
    models = _pool(ctx, free_only=free_only, vision=role == "media")
    if not 0 <= index < len(models):
        view = pick(ctx, role, 1, free_only)
        view.alert = "that model is gone from the catalog; the list has been reloaded"
        return view

    model = models[index]
    settings_store.set_override(ctx.rt.db, ROLES[role], model.id, by=ctx.user.id)
    audit(ctx.rt, ctx.user, "model_set", f"{role}={model.id}")

    view = overview(ctx)
    view.text = f"✅ {role} now uses {model.id}\n\n{view.text}"
    view.alert = f"{role}: {model.short}"
    view.extras["reload"] = True
    return view


def ask_search(ctx, role: str, free_only: bool) -> View:
    view = pick(ctx, role, 1, free_only)
    view.text = f"Send part of a model name to filter the list.\n\n{view.text}"
    view.prompt = ("mdlfind", f"{role}:{_flag(free_only)}")
    return view


def take_search(ctx, target: str, text: str) -> View:
    """Show what matches. Assigning still goes through the numbered list."""
    from ..catalog import search

    role, _, flag = target.partition(":")
    free_only = flag != "0"
    if role not in ROLES:
        return overview(ctx)

    models = _pool(ctx, free_only=free_only, vision=role == "media")
    found = search(models, text)
    if not found:
        view = pick(ctx, role, 1, free_only)
        view.text = f"nothing matching {text!r}\n\n{view.text}"
        return view

    lines = [f"🔎 {len(found)} matching {text!r}\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for model in found[:8]:
        lines.append(f"{model.short}  {model.window}  {model.marks}  {model.price}")
        rows.append(
            [
                button(
                    f"{trim(model.short, 30)} · {model.window}",
                    "mdl",
                    "set",
                    role,
                    _flag(free_only),
                    models.index(model),
                )
            ]
        )
    rows.append(back_row("mdl"))
    return View("\n".join(lines), keyboard(*rows))


# -- what has just appeared ------------------------------------------------
NEW_SHOWN = 12
# A model listed for the first time within this long is worth a badge. Services
# add models weekly, so a week is what "new" is worth calling.
NEW_FOR = 7 * 86400


def whats_new(ctx) -> View:
    """Models that have appeared since this install started watching.

    Free tiers gain and lose models weekly and the only way anyone noticed used
    to be a 404 in the log. This is the same information, before it breaks.
    """
    rows = ctx.rt.registry.newest_models(NEW_SHOWN) if ctx.rt.registry else []
    if not rows:
        view = overview(ctx)
        view.alert = "nothing listed yet; press sync"
        return view

    cutoff = time.time() - NEW_FOR
    recent = [row for row in rows if (row["first_seen"] or 0) >= cutoff]
    lines = ["🆕 New models\n"]
    if recent:
        lines.append(f"{len(recent)} appeared in the last week.\n")
    else:
        lines.append("Nothing new this week. The most recent are:\n")

    for row in rows:
        marks = " 🖼" if row["vision"] else ""
        price = "free" if row["free"] else "paid"
        fresh = "🆕 " if (row["first_seen"] or 0) >= cutoff else ""
        lines.append(
            f"{fresh}{row['model'].split('/', 1)[-1]}{marks}\n"
            f"   {row['service']} · {_tokens(row['context'])} · {price}"
            f" · first seen {ago(row['first_seen'])}"
        )

    lines.append("\nAssign one from its job screen; free mode uses the pool by itself.")
    return View(
        "\n".join(lines),
        keyboard([button("🔄 scan again", "mdl", "rescan")], back_row("mdl")),
    )


# -- what the models did --------------------------------------------------
def usage(ctx) -> View:
    """Tokens and calls per model today. Free models all cost nothing, so the
    interesting number is the work, not the money."""
    rows = ctx.rt.budget.model_usage()
    if not rows:
        view = overview(ctx)
        view.alert = "no model calls recorded today"
        return view

    total_calls = sum(r["calls"] for _, r in rows)
    total_in = sum(r["prompt"] for _, r in rows)
    total_out = sum(r["completion"] for _, r in rows)
    total_cost = sum(r["cost"] for _, r in rows)

    lines = [
        "📊 Today, by model\n",
        f"{total_calls} calls · {_tokens(total_in)} in / {_tokens(total_out)} out"
        f" · ${total_cost:.4f}\n",
    ]
    for model, row in rows[:12]:
        cost = f" · ${row['cost']:.4f}" if row["cost"] else ""
        lines.append(
            f"{model.split('/', 1)[-1]}\n"
            f"   {row['calls']} calls · {_tokens(row['prompt'])} in"
            f" / {_tokens(row['completion'])} out{cost}"
        )
    if len(rows) > 12:
        lines.append(f"\n…and {len(rows) - 12} more")

    return View("\n".join(lines), keyboard([button("🔄 refresh", "mdl", "u")], back_row("mdl")))


async def sync(ctx) -> View:
    """Read the catalog again, for when a service adds or retires models."""
    try:
        await ctx.rt.llm.load_catalog()
    except Exception as exc:
        log.warning("could not reload the catalog: %s", exc)
        view = overview(ctx)
        view.alert = f"could not read the catalog: {exc}"
        view.extras["failed"] = True
        return view

    free = len(_pool(ctx, free_only=True))
    every = len(_pool(ctx, free_only=False))
    view = overview(ctx)
    view.alert = f"{free} free of {every} chat models"
    return view


async def resync(ctx) -> View:
    """Read the catalog again and stay on the new-models screen to see the result."""
    view = await sync(ctx)
    if view.extras.get("failed"):
        return view
    fresh = whats_new(ctx)
    fresh.alert = view.alert
    return fresh
