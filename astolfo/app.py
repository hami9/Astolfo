"""Application wiring and lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ChatType
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)

from . import (
    __version__,
    admin,
    chat,
    commands,
    donate,
    master,
    media,
    membership,
    runtime,
    server_ops,
    settings_store,
)
from .config import ConfigError, Settings
from .runtime import Runtime

log = logging.getLogger(__name__)

AUTOSAVE_INTERVAL = 120


class _AliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"Astolfo is alive~"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def start_keepalive(port: int) -> None:
    """Tiny HTTP endpoint so uptime pingers can keep a free host awake."""

    def serve() -> None:
        try:
            # Binds publicly on purpose: an uptime pinger has to reach it.
            HTTPServer(("0.0.0.0", port), _AliveHandler).serve_forever()  # noqa: S104
        except Exception as exc:
            log.warning("keepalive server did not start: %s", exc)

    threading.Thread(target=serve, daemon=True, name="keepalive").start()
    log.info("keepalive server listening on port %s", port)


async def _autosave(rt: Runtime) -> None:
    while True:
        await asyncio.sleep(AUTOSAVE_INTERVAL)
        rt.save()


async def post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    rt = Runtime.build(settings, app.bot_data.get("db"), app.bot_data.get("box"))
    app.bot_data[runtime.KEY] = rt

    # First line in the log, so a bug report says which version produced it.
    log.info("astolfo %s on python %s", __version__, platform.python_version())
    await rt.llm.load_catalog()
    # media rows resolve with the modality flags the real turn would carry, so the
    # startup log names the model that will actually read an image or a voice note
    for label, model, kwargs in (
        ("fast", settings.model_fast, {}),
        ("think", settings.model_think, {}),
        ("search", settings.model_search, {}),
        ("image", settings.model_media, {"vision": True}),
        ("audio", settings.model_media, {"audio": True}),
        ("router", settings.model_router, {}),
    ):
        log.info("model[%s] -> %s", label, rt.llm.resolve(model, **kwargs))

    if not media.ffmpeg_available():
        log.warning("ffmpeg not found: voice is skipped and videos fall back to thumbnails")
    if settings.daily_budget_usd > 0:
        log.info(
            "daily budget $%.2f, spent today $%.4f",
            settings.daily_budget_usd,
            rt.budget.today_cost(),
        )

    with contextlib.suppress(Exception):
        await app.bot.set_my_commands(commands.COMMANDS)
    await _offer_panel(app, rt)

    me = await app.bot.get_me()
    log.info("started as @%s (%s)", me.username, me.id)
    await _report_restart(app, rt)
    app.bot_data["autosave"] = asyncio.create_task(_autosave(rt))


async def _offer_panel(app: Application, rt: Runtime) -> None:
    """Show /panel in the owner's chat only, so nobody else sees it suggested."""
    owner = master.current(rt)
    log.info("owner: %s", master.describe(rt))
    if not owner:
        return
    with contextlib.suppress(Exception):
        await app.bot.set_my_commands(
            [*commands.COMMANDS, BotCommand("panel", "owner controls")],
            scope=BotCommandScopeChat(chat_id=owner),
        )


async def _report_restart(app: Application, rt: Runtime) -> None:
    """Tell whoever asked for an update how it went, once the bot is back."""
    who = rt.db.note("report_to")
    if not who:
        return
    rt.db.clear_note("report_to")

    outcome = server_ops.last_result(rt.settings.data_dir) or "back up"
    with contextlib.suppress(Exception):
        await app.bot.send_message(
            chat_id=int(who), text=f"✅ back up~\n{outcome}\nrunning {server_ops.commit()}"
        )


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("autosave")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    rt: Runtime = app.bot_data.get(runtime.KEY)
    if rt:
        await rt.aclose()
    log.info("shutdown complete")


CONTENT_FILTER = (
    filters.TEXT
    | filters.PHOTO
    | filters.Sticker.ALL
    | filters.ANIMATION
    | filters.VIDEO
    | filters.VIDEO_NOTE
    | filters.VOICE
    | filters.AUDIO
    | filters.Document.IMAGE
    | filters.Document.VIDEO
    | filters.Document.AUDIO
)


async def _dormant_guard(update: Update, context) -> None:
    """A group switched off from the panel hears nothing, commands included.

    This runs before every other handler, so there is one place where "off"
    means off rather than a check repeated in each of them.
    """
    chat = update.effective_chat
    if chat is None or chat.type == ChatType.PRIVATE:
        return
    if chat.id in runtime.get(context).dormant:
        raise ApplicationHandlerStop


def build_application(settings: Settings, database=None, box=None) -> Application:
    builder = (
        ApplicationBuilder()
        .token(settings.telegram_token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    with contextlib.suppress(Exception):  # needs the rate-limiter extra
        builder = builder.rate_limiter(AIORateLimiter())

    app = builder.build()
    app.bot_data["settings"] = settings
    app.bot_data["db"] = database
    app.bot_data["box"] = box

    for name, handler in (
        ("start", commands.start),
        ("help", commands.help_),
        ("about", commands.about),
        ("reset", commands.reset),
        ("chance", commands.chance),
        ("mode", commands.mode),
        ("mute", commands.mute),
        ("unmute", commands.unmute),
        ("status", commands.status),
        ("usage", commands.usage),
        ("donate", donate.donate),
        ("panel", admin.open_panel),
    ):
        app.add_handler(CommandHandler(name, handler))

    # Group -2, ahead of everything: a switched-off group is dropped whole.
    app.add_handler(TypeHandler(Update, _dormant_guard), group=-2)

    app.add_handler(CallbackQueryHandler(admin.on_button, pattern=admin.PATTERN))
    # Group -1 so a typed answer to the panel is seen before the chat pipeline,
    # which is what a message in the owner's private chat would otherwise be.
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, admin.on_text), group=-1
    )

    app.add_handler(
        ChatMemberHandler(membership.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    app.add_handler(PreCheckoutQueryHandler(donate.precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, donate.paid))

    app.add_handler(MessageHandler(CONTENT_FILTER & ~filters.COMMAND, chat.handle_message))
    app.add_error_handler(chat.on_error)
    return app


def run() -> None:
    try:
        settings, database, box = settings_store.bootstrap()
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from None

    if settings.keepalive:
        start_keepalive(settings.keepalive_port)
    build_application(settings, database, box).run_polling(
        drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
    )
