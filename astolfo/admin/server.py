"""The server screen: how the machine is doing, and the two jobs that need root."""

from __future__ import annotations

import os

from .. import server_ops
from .guard import audit
from .sections import View
from .ui import back_row, button, confirm_rows, keyboard


def overview(ctx) -> View:
    used_ram, total_ram = server_ops.memory()
    used_disk, total_disk = server_ops.disk()
    data_dir = ctx.rt.settings.data_dir

    lines = [
        "🖥 Server\n",
        f"service: {server_ops.service_state()}",
        f"uptime: {server_ops.uptime()}",
        f"load: {server_ops.load()}",
        f"memory: {used_ram}/{total_ram} MB",
        f"disk: {used_disk}/{total_disk} GB",
        f"running: {server_ops.commit()}",
    ]
    if not server_ops.agent_installed(data_dir):
        lines.append("\n⚠️ the update helper is not installed; run deploy/vps-setup.sh again")

    last = server_ops.last_result(data_dir)
    if last and server_ops.result_age(data_dir) < 3600:
        lines.append(f"\nlast job: {last[-300:]}")

    return View(
        "\n".join(lines),
        keyboard(
            [button("🔄 check for updates", "srv", "check")],
            [button("⬆️ update and restart", "srv", "update")],
            [button("♻️ restart", "srv", "restart")],
            [button("📄 log", "srv", "log")],
            back_row(),
        ),
    )


def check(ctx) -> View:
    count, detail = server_ops.updates_available()
    view = overview(ctx)
    view.alert = detail
    view.text = f"{'⬆️' if count else '✅'} {detail}\n\n{view.text}"
    return view


def log(ctx, *, errors_only: bool = False, skip: int = 0) -> View:
    """The service log, a page at a time, with a file for when a page is not enough."""
    page = server_ops.journal(errors_only=errors_only, skip=skip)
    where = f", {skip} lines back" if skip else ""
    head = f"{'⚠️ errors only' if errors_only else '📄 recent log'}{where}"
    flag = "1" if errors_only else "0"
    older = skip + server_ops.SCREEN_LINES
    rows = [
        [
            button("⬅️ older", "srv", "log", flag, str(older)),
            button("➡️ newer", "srv", "log", flag,
                   str(max(0, skip - server_ops.SCREEN_LINES))),
        ],
        [
            button("📄 all" if errors_only else "⚠️ errors only", "srv", "log",
                   "0" if errors_only else "1", "0"),
            button("📎 as a file", "srv", "logfile", flag),
        ],
        back_row("srv"),
    ]
    return View(f"{head}\n\n{page}", keyboard(*rows))


def log_file(ctx, *, errors_only: bool = False) -> View:
    """The last few thousand lines as an attachment.

    A message holds about four thousand characters and a log worth reading is
    longer than that, which is why the screen kept cutting off before the part
    that mattered.
    """
    name = "astolfo-errors.log" if errors_only else "astolfo.log"
    path = server_ops.journal_file(
        os.path.join(ctx.rt.settings.data_dir, name), errors_only=errors_only
    )
    view = log(ctx, errors_only=errors_only)
    if not path:
        view.alert = "the log is not readable from here"
        return view
    view.document = path
    view.alert = "sent as a file"
    return view


def job(ctx, action: str, confirmed: bool) -> View:
    if action not in server_ops.ACTIONS:
        return overview(ctx)

    if not confirmed:
        wording = {
            "restart": "Restart the bot?\nIt goes quiet for a few seconds.",
            "update": (
                "Pull the latest code and restart?\n"
                "If the new version fails to start, the server rolls back by itself."
            ),
        }[action]
        return View(wording, keyboard(*confirm_rows(action, "srv", f"{action}!")))

    ok, detail = server_ops.request(ctx.rt.settings.data_dir, action)
    audit(ctx.rt, ctx.user, action, detail)
    if action == "update":
        # The bot is about to be replaced by the new process, so leave a note for
        # it to deliver once it is back.
        ctx.rt.db.set_note("report_to", str(ctx.user.id))

    view = overview(ctx)
    view.alert = detail
    view.text = f"{'⏳' if ok else '❌'} {detail}\n\n{view.text}"
    return view
