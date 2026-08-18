"""The services screen: what is configured, what is working, and what to press.

Everything shown here comes from state the bot already keeps while it works —
counters written on each call, rest windows written when a service turns it away.
Opening this screen costs no API calls at all; only the test buttons spend one.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton

from .. import providers as providers_mod
from ..crypto import SecretsUnavailable
from .guard import audit
from .sections import View
from .ui import ago, back_row, button, confirm_rows, keyboard, trim

log = logging.getLogger(__name__)

WORKING = "✅"
RESTING = "💤"
NO_KEY = "—"
OFF = "🚫"


def _known(ctx) -> list[str]:
    """Every service worth showing: the presets, plus anything saved."""
    stored = [row["name"] for row in ctx.rt.db.services()]
    return list(dict.fromkeys([*providers_mod.PRESETS, *stored]))


def _state(ctx, name: str) -> tuple[str, str]:
    """The mark and the words for a service's current condition."""
    row = ctx.rt.db.service(name)
    keys = ctx.rt.db.credentials(name)
    live = next((p for p in ctx.rt.llm.providers if p.name == name), None)

    if row is not None and not row["enabled"]:
        return OFF, "switched off here"
    if live is None:
        return NO_KEY, "no key"

    resting = float(row["rested_until"]) if row is not None else 0.0
    if resting > time.time():
        minutes = (resting - time.time()) / 60
        detail = f"resting for {minutes:.0f} more minutes"
        if row is not None and row["last_error"]:
            detail += f" — {trim(row['last_error'], 40)}"
        return RESTING, detail

    usable = sum(1 for key in keys if key["enabled"] and key["rested_until"] <= time.time())
    usable += len([c for c in live.credentials if c.id is None])
    return (WORKING, f"{usable} key(s) ready") if usable else (NO_KEY, "no usable key")


# -- the list -------------------------------------------------------------
def overview(ctx) -> View:
    usage = ctx.rt.registry.usage_today()
    lines = ["🔌 Services\n"]
    rows: list[list[InlineKeyboardButton]] = []

    for name in _known(ctx):
        mark, detail = _state(ctx, name)
        today = usage.get(name)
        counted = ""
        if today:
            counted = f" · {today['requests']} calls"
            if today["failures"]:
                counted += f", {today['failures']} failed"
        preset = providers_mod.PRESETS.get(name)
        note = f" ({preset.note})" if preset and preset.note else ""
        lines.append(f"{mark} {name}{note} — {detail}{counted}")
        rows.append([button(f"{mark} {name}", "svc", "s", name)])

    lines.append("\nThey are tried top to bottom. The first that answers wins.")
    return View(
        "\n".join(lines),
        keyboard(
            *rows,
            [button("🧪 test all", "svc", "testall"), button("➕ add a service", "svc", "new")],
            back_row(),
        ),
    )


async def test_all(ctx) -> View:
    lines = ["🧪 Test\n"]
    for provider in ctx.rt.llm.providers:
        ok, detail = await ctx.rt.llm.probe(provider.name)
        lines.append(f"{'✅' if ok else '❌'} {provider.name}: {detail}")
    if len(lines) == 1:
        lines.append("no service has a key yet")

    view = overview(ctx)
    view.text = "\n".join(lines) + "\n\n" + view.text
    return view


# -- one service ----------------------------------------------------------
def detail(ctx, name: str) -> View:
    rt = ctx.rt
    row = rt.db.service(name)
    preset = providers_mod.PRESETS.get(name)
    live = next((p for p in rt.llm.providers if p.name == name), None)
    mark, state = _state(ctx, name)
    today = rt.registry.usage_today().get(name)

    base = (row["base_url"] if row and row["base_url"] else "") or (
        preset.base_url if preset else "?"
    )
    models = live.models if live else []
    lines = [
        f"{mark} {name}\n",
        f"state: {state}",
    ]
    if preset and preset.note:
        lines.append(f"billing: {preset.note}")
    lines += [
        f"endpoint: {base}",
        f"models: {', '.join(models) if models else 'discovered from the service'}",
    ]
    if today:
        lines.append(
            f"today: {today['requests']} calls, {today['failures']} failed, "
            f"{today['tokens']} tokens, ${today['cost']:.4f}"
        )
    if live and any(c.id is None for c in live.credentials):
        lines.append("one key comes from .env")
    if not rt.db.credentials(name) and preset and preset.signup:
        lines.append(f"get a key at {preset.signup}")

    lines.append("\nkeys:")
    rows: list[list[InlineKeyboardButton]] = []
    keys = rt.db.credentials(name)
    if not keys:
        lines.append("• none saved here")
    for key in keys:
        mark_key = "✅" if key["enabled"] else "🚫"
        if key["enabled"] and key["rested_until"] > time.time():
            mark_key = "💤"
        label = key["label"] or f"key {key['id']}"
        lines.append(
            f"{mark_key} {label} — {key['requests']} calls, {key['failures']} failed"
            + (f", last used {ago(key['last_used'])}" if key["last_used"] else "")
        )
        rows.append([button(f"{mark_key} {trim(label, 20)}", "svc", "k", key["id"])])

    enabled = row is None or bool(row["enabled"])
    controls = [
        [
            button("➕ add a key", "svc", "s", name, "addkey"),
            button("🧪 test", "svc", "s", name, "test"),
        ],
        [
            button("⏻ off" if enabled else "⏻ on", "svc", "s", name, "off" if enabled else "on"),
            button("⬆️", "svc", "s", name, "up"),
            button("⬇️", "svc", "s", name, "down"),
        ],
        [button("⏰ wake it now", "svc", "s", name, "wake")],
        [
            button("✏️ models", "svc", "s", name, "models"),
            button("✏️ endpoint", "svc", "s", name, "url"),
        ],
    ]
    if preset is None:
        controls.append([button("🗑 delete this service", "svc", "s", name, "del")])
    return View("\n".join(lines), keyboard(*controls, back_row("svc")))


async def test(ctx, name: str) -> View:
    ok, note = await ctx.rt.llm.probe(name)
    view = detail(ctx, name)
    view.alert = f"{'✅' if ok else '❌'} {note}"
    view.text = f"{'✅' if ok else '❌'} {note}\n\n{view.text}"
    return view


def set_enabled(ctx, name: str, enabled: bool) -> View:
    ctx.rt.registry.set_service_enabled(name, enabled)
    audit(ctx.rt, ctx.user, "service_on" if enabled else "service_off", name)
    view = detail(ctx, name)
    view.alert = f"{name} is {'on' if enabled else 'off'}"
    view.extras["reload"] = True
    return view


def move(ctx, name: str, direction: int) -> View:
    ctx.rt.registry.move(name, direction)
    audit(ctx.rt, ctx.user, "service_move", f"{name} {direction:+d}")
    view = overview(ctx)
    view.alert = "order changed"
    view.extras["reload"] = True
    return view


def wake(ctx, name: str) -> View:
    ctx.rt.registry.wake(name)
    audit(ctx.rt, ctx.user, "service_wake", name)
    view = detail(ctx, name)
    view.alert = "it will be tried again on the next message"
    view.extras["reload"] = True
    return view


def delete(ctx, name: str, confirmed: bool) -> View:
    if providers_mod.PRESETS.get(name) is not None:
        view = detail(ctx, name)
        view.alert = "this one is built in; switch it off instead"
        return view
    if not confirmed:
        return View(
            f"Delete {name} and its keys?\nThe keys cannot be recovered afterwards.",
            keyboard(*confirm_rows(f"delete {name}", "svc", "s", name, "del!")),
        )

    ctx.rt.registry.delete_service(name)
    audit(ctx.rt, ctx.user, "service_delete", name)
    view = overview(ctx)
    view.alert = f"{name} removed"
    view.extras["reload"] = True
    return view


# -- one key --------------------------------------------------------------
def key_detail(ctx, credential_id: int) -> View:
    row = ctx.rt.db.credential(credential_id)
    if row is None:
        return View("that key is gone", keyboard(back_row("svc")))

    masked = _mask_stored(ctx, credential_id)
    label = row["label"] or f"key {row['id']}"
    lines = [
        f"🔑 {label}\n",
        f"service: {row['service']}",
        f"key: {masked}",
        f"state: {'on' if row['enabled'] else 'off'}",
        f"calls: {row['requests']}, failed: {row['failures']}",
        f"last used: {ago(row['last_used'])}",
        f"last worked: {ago(row['last_ok'])}",
    ]
    if row["last_error"]:
        lines.append(f"last error: {trim(row['last_error'], 80)}")
    if row["rested_until"] > time.time():
        lines.append(f"resting for {(row['rested_until'] - time.time()) / 3600:.1f} more hours")

    enabled = bool(row["enabled"])
    return View(
        "\n".join(lines),
        keyboard(
            [
                button(
                    "⏻ off" if enabled else "⏻ on",
                    "svc", "k", credential_id, "off" if enabled else "on",
                ),
                button("🗑 remove", "svc", "k", credential_id, "rm"),
            ],
            back_row("svc", "s", row["service"]),
        ),
    )


def _mask_stored(ctx, credential_id: int) -> str:
    from ..crypto import mask

    return mask(ctx.rt.registry.reveal(credential_id))


def key_enabled(ctx, credential_id: int, enabled: bool) -> View:
    ctx.rt.registry.set_key_enabled(credential_id, enabled)
    audit(ctx.rt, ctx.user, "key_on" if enabled else "key_off", str(credential_id))
    view = key_detail(ctx, credential_id)
    view.alert = f"key is {'on' if enabled else 'off'}"
    view.extras["reload"] = True
    return view


def key_remove(ctx, credential_id: int, confirmed: bool) -> View:
    row = ctx.rt.db.credential(credential_id)
    if row is None:
        return View("that key is gone", keyboard(back_row("svc")))
    if not confirmed:
        return View(
            f"Remove this key from {row['service']}?",
            keyboard(*confirm_rows("remove it", "svc", "k", credential_id, "rm!")),
        )

    service = row["service"]
    ctx.rt.registry.remove_key(credential_id)
    audit(ctx.rt, ctx.user, "key_remove", f"{service}#{credential_id}")
    view = detail(ctx, service)
    view.alert = "key removed"
    view.extras["reload"] = True
    return view


# -- typed answers --------------------------------------------------------
def ask_key(ctx, name: str) -> View:
    return View(
        f"Send the key for {name}.\n"
        "It is saved encrypted and your message is deleted straight away.\n"
        "Add a label first if you like, as `label: the-key`.\n\n"
        "Send /cancel to stop.",
        keyboard(back_row("svc", "s", name)),
        prompt=("svckey", name),
    )


def take_key(ctx, name: str, text: str) -> View:
    label, _, value = text.partition(":")
    if not value.strip():
        label, value = "", text
    try:
        ctx.rt.registry.add_key(name, value.strip(), label=label.strip()[:40])
    except SecretsUnavailable as exc:
        view = detail(ctx, name)
        view.text = f"❌ {exc}\n\n{view.text}"
        return view

    audit(ctx.rt, ctx.user, "key_add", name)
    view = detail(ctx, name)
    view.text = f"✅ key added to {name}\n\n{view.text}"
    view.extras["reload"] = True
    return view


def ask_new_service(ctx) -> View:
    return View(
        "Add any OpenAI-compatible service.\n\n"
        "Send it as three parts on one line:\n"
        "`name url model,model`\n\n"
        "For example:\n"
        "together https://api.together.xyz/v1 meta-llama/Llama-3.3-70B-Instruct-Turbo\n\n"
        "The key comes next, after it is added.",
        keyboard(back_row("svc")),
        prompt=("svcnew", "*"),
    )


def take_new_service(ctx, text: str) -> View:
    parts = text.split()
    if len(parts) < 2:
        view = overview(ctx)
        view.text = "❌ I need at least a name and a URL\n\n" + view.text
        return view

    name = parts[0].strip().lower()
    url = parts[1].strip()
    models = [m.strip() for m in " ".join(parts[2:]).replace(",", " ").split() if m.strip()]

    if not url.startswith("https://") and not url.startswith("http://"):
        view = overview(ctx)
        view.text = f"❌ {url} is not a URL\n\n" + view.text
        return view
    if providers_mod.PRESETS.get(name) is not None:
        view = detail(ctx, name)
        view.text = f"❌ {name} is built in already\n\n{view.text}"
        return view

    ctx.rt.registry.add_service(name, url, models)
    audit(ctx.rt, ctx.user, "service_add", f"{name} {url}")
    view = detail(ctx, name)
    view.text = f"✅ {name} added — give it a key next\n\n{view.text}"
    view.extras["reload"] = True
    return view


def ask_field(ctx, name: str, field: str) -> View:
    live = next((p for p in ctx.rt.llm.providers if p.name == name), None)
    if field == "models":
        current = ", ".join(live.models) if live and live.models else "(discovered)"
        return View(
            f"Send the models for {name}, separated by commas.\nNow: {current}\n\n"
            "Send `-` to go back to the built-in list.",
            keyboard(back_row("svc", "s", name)),
            prompt=("svcmodels", name),
        )
    current = live.base_url if live else "?"
    return View(
        f"Send the endpoint for {name}.\nNow: {current}\n\n"
        "It should end in /v1 for most services.",
        keyboard(back_row("svc", "s", name)),
        prompt=("svcurl", name),
    )


def take_field(ctx, name: str, field: str, text: str) -> View:
    value = text.strip()
    if field == "models":
        models = "" if value == "-" else ",".join(
            m.strip() for m in value.replace(",", " ").split() if m.strip()
        )
        ctx.rt.registry.edit_service(name, models=models)
        note = "models updated" if models else "back to the built-in list"
    else:
        if not value.startswith("http"):
            view = detail(ctx, name)
            view.text = f"❌ {value} is not a URL\n\n{view.text}"
            return view
        ctx.rt.registry.edit_service(name, base_url=value)
        note = "endpoint updated"

    audit(ctx.rt, ctx.user, "service_edit", f"{name} {field}")
    view = detail(ctx, name)
    view.text = f"✅ {note}\n\n{view.text}"
    view.extras["reload"] = True
    return view
