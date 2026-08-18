"""Application wiring and lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from . import chat, commands, donate, media, membership, runtime
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
            HTTPServer(("0.0.0.0", port), _AliveHandler).serve_forever()
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
    rt = Runtime.build(settings)
    app.bot_data[runtime.KEY] = rt

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

    me = await app.bot.get_me()
    log.info("started as @%s (%s)", me.username, me.id)
    app.bot_data["autosave"] = asyncio.create_task(_autosave(rt))


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


def build_application(settings: Settings) -> Application:
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

    for name, handler in (
        ("start", commands.start),
        ("help", commands.help_),
        ("reset", commands.reset),
        ("chance", commands.chance),
        ("mode", commands.mode),
        ("mute", commands.mute),
        ("unmute", commands.unmute),
        ("status", commands.status),
        ("usage", commands.usage),
        ("donate", donate.donate),
    ):
        app.add_handler(CommandHandler(name, handler))

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
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from None

    if settings.keepalive:
        start_keepalive(settings.keepalive_port)
    build_application(settings).run_polling(
        drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
    )
