"""Who this bot belongs to.

These values are fixed in the source on purpose. They are shown, never acted on:
nothing here grants access, and the panel is guarded by a numeric Telegram id, so
a wrong value would be a wrong caption and nothing more. The settings layer
refuses to override any of them and a test pins the exact strings, so the credit
cannot drift away by accident or by a stray row in the database.
"""

from __future__ import annotations

CHANNEL = "ssh_to_mylinux"
CHANNEL_URL = f"https://t.me/{CHANNEL}"
CREATOR = "ham1235i"
CREATOR_URL = f"https://t.me/{CREATOR}"

CREDIT_EN = f"made by @{CREATOR} · channel @{CHANNEL}"
CREDIT_FA = f"ساخته‌ی @{CREATOR} · کانال @{CHANNEL}"

ABOUT_EN = (
    "I'm Astolfo~ a chat bot living in Telegram groups.\n\n"
    f"📣 channel: {CHANNEL_URL}\n"
    f"👤 made by: {CREATOR_URL}\n"
    "🧠 I read photos, GIFs, videos and voice messages, and I only ever answer in text\n"
    "💛 /donate keeps my API bill paid\n\n"
    f"{CREDIT_EN}"
)
ABOUT_FA = (
    "من آستولفوام~ یه ربات چت که تو گروه‌های تلگرام زندگی می‌کنه.\n\n"
    f"📣 کانال: {CHANNEL_URL}\n"
    f"👤 سازنده: {CREATOR_URL}\n"
    "🧠 عکس و گیف و ویدیو می‌بینم، ویس گوش می‌دم، و فقط متن جواب می‌دم\n"
    "💛 با /donate خرج ای‌پی‌آیم رو می‌دی\n\n"
    f"{CREDIT_FA}"
)


def about(locale: str = "en") -> str:
    return ABOUT_FA if locale == "fa" else ABOUT_EN


def credit(locale: str = "en") -> str:
    return CREDIT_FA if locale == "fa" else CREDIT_EN
