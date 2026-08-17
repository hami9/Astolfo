"""ساخت و راه‌اندازی اپلیکیشن تلگرام."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import BotCommand, Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import handlers, media
from .ai import AIClient
from .config import Settings
from .memory import ChatStore

log = logging.getLogger("astolfo.app")

COMMANDS = [
    BotCommand("start", "سلام کن به آستولفو"),
    BotCommand("help", "راهنما"),
    BotCommand("chance", "احتمال ورود خودکار به بحث"),
    BotCommand("mode", "auto | fast | think | search"),
    BotCommand("status", "وضعیت فعلی"),
    BotCommand("reset", "پاک کردن حافظهٔ این چت"),
    BotCommand("mute", "ساکت شو"),
    BotCommand("unmute", "برگرد"),
]


# ---------------------------------------------------------------------------
# سرور کوچک زنده‌نگه‌دار (برای Replit / سرویس‌های پینگ)
# ---------------------------------------------------------------------------
class _AliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = "Astolfo is alive~ 🐰".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # لاگ HTTP را ساکت می‌کنیم
        return


def start_keepalive(port: int) -> None:
    def run() -> None:
        try:
            HTTPServer(("0.0.0.0", port), _AliveHandler).serve_forever()
        except Exception as exc:  # pragma: no cover
            log.warning("سرور زنده‌نگه‌دار بالا نیامد: %s", exc)

    threading.Thread(target=run, daemon=True, name="keepalive").start()
    log.info("سرور زنده‌نگه‌دار روی پورت %s بالا آمد.", port)


# ---------------------------------------------------------------------------
# چرخهٔ عمر
# ---------------------------------------------------------------------------
async def _autosave(store: ChatStore) -> None:
    while True:
        await asyncio.sleep(120)
        store.save()


async def post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    ai = AIClient(settings)
    app.bot_data["ai"] = ai
    app.bot_data["store"] = ChatStore(settings)

    await ai.load_model_catalog()
    for label, model in (
        ("سریع", settings.model_fast),
        ("تفکر", settings.model_think),
        ("سرچ", settings.model_search),
        ("رسانه", settings.model_media),
        ("مسیریاب", settings.model_router),
    ):
        log.info("مدل %s → %s", label, ai.resolve_model(model))

    if not media.ffmpeg_available():
        log.warning(
            "ffmpeg پیدا نشد: ویس تحلیل نمی‌شود و از ویدیو فقط تصویر بندانگشتی دیده می‌شود."
        )

    with contextlib.suppress(Exception):
        await app.bot.set_my_commands(COMMANDS)

    me = await app.bot.get_me()
    log.info("ربات @%s (%s) بالا آمد.", me.username, me.id)

    # asyncio مستقیم، چون هنوز اپلیکیشن در حالت running نیست
    app.bot_data["autosave_task"] = asyncio.create_task(_autosave(app.bot_data["store"]))


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("autosave_task")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    store: ChatStore = app.bot_data.get("store")
    if store:
        store.save(force=True)
    ai: AIClient = app.bot_data.get("ai")
    if ai:
        await ai.aclose()
    log.info("خاموش شد. 👋")


def build_application(settings: Settings) -> Application:
    builder = (
        ApplicationBuilder()
        .token(settings.telegram_token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    with contextlib.suppress(Exception):  # نیازمند اکسترای rate-limiter
        builder = builder.rate_limiter(AIORateLimiter())

    app = builder.build()
    app.bot_data["settings"] = settings

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("reset", handlers.cmd_reset))
    app.add_handler(CommandHandler("chance", handlers.cmd_chance))
    app.add_handler(CommandHandler("mode", handlers.cmd_mode))
    app.add_handler(CommandHandler("mute", handlers.cmd_mute))
    app.add_handler(CommandHandler("unmute", handlers.cmd_unmute))
    app.add_handler(CommandHandler("status", handlers.cmd_status))

    content = (
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
    app.add_handler(MessageHandler(content & ~filters.COMMAND, handlers.handle_message))
    app.add_error_handler(handlers.on_error)
    return app


def run() -> None:
    settings = Settings.from_env()
    if settings.keepalive:
        start_keepalive(settings.keepalive_port)
    app = build_application(settings)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
