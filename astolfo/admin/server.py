"""The server screen: how the machine is doing, and the two jobs that need root."""

from __future__ import annotations

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


def log(ctx) -> View:
    return View(f"📄 recent log\n\n{server_ops.journal()}", keyboard(back_row("srv")))


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
