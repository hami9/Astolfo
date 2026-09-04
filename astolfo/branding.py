"""Who this bot belongs to.

These values are fixed in the source on purpose. They are shown, never acted on:
nothing here grants access, and the panel is guarded by a numeric Telegram id, so
a wrong value would be a wrong caption and nothing more. The settings layer
refuses to override any of them and a test pins the exact strings, so the credit
cannot drift away by accident or by a stray row in the database.
"""

from __future__ import annotations

CHANNEL = "hami294"
CHANNEL_URL = f"https://t.me/{CHANNEL}"
CREATOR = "ham1235i"
CREATOR_URL = f"https://t.me/{CREATOR}"
SITE = "hami9.ir"
SITE_URL = f"https://{SITE}"
# An invite code, not a handle: there is nothing to compose it from.
DISCORD_URL = "https://discord.gg/K33PnNafcD"
REPO_URL = "https://github.com/hami9/Astolfo"
LICENSE = "MIT"

CREDIT_EN = f"made by @{CREATOR} · channel @{CHANNEL} · {SITE}"
CREDIT_FA = f"ساخته‌ی @{CREATOR} · کانال @{CHANNEL} · {SITE}"

# What the bot is and is not, for anyone who asks. It deliberately never names
# the model it happens to be running on or whose API is paying for it: that is
# an operator's business, it changes week to week, and the answer would be stale
# by the time somebody read it. Which model runs which job is on the owner's
# panel instead.
SOURCE_EN = (
    "🧩 open source\n"
    f"All of my code is public, under the {LICENSE} licence:\n"
    f"{REPO_URL}\n"
    "Anyone can read it, change it, and run their own copy. You bring your own bot "
    "token and your own API key and it is yours — the setup script, a Docker image "
    "and the docs are all in there.\n\n"
    "✅ what I can do\n"
    "• always answer a reply, a mention, or my name; the rest of the time I jump in "
    "on my own\n"
    "• look at photos, stickers, GIFs and videos, and listen to voice messages\n"
    "• think longer about a hard question, and search the web when a fact has to be "
    "fresh\n"
    "• remember the conversation, and pick up how this chat likes to be talked to\n\n"
    "🚫 what I can't do\n"
    "• send anything but text — no images, audio, video or stickers\n"
    "• tell who a person in a picture is, and I will not guess\n"
    "• see a chat I am not in, or anything said before I joined\n"
    "• be right about everything. When I do not know, I say so instead of inventing it"
)
SOURCE_FA = (
    "🧩 متن‌باز\n"
    f"همهٔ کدهام عمومیه، با لایسنس {LICENSE}:\n"
    f"{REPO_URL}\n"
    "هرکسی می‌تونه بخونتش، تغییرش بده و نسخهٔ خودش رو بالا بیاره. توکن ربات و کلید "
    "ای‌پی‌آی خودت رو می‌ذاری و مال خودته — اسکریپت نصب، ایمیج داکر و مستندات همه اونجاست.\n\n"
    "✅ چیکارا بلدم\n"
    "• به ریپلای و منشن و اسمم همیشه جواب می‌دم، بقیهٔ وقتا خودم می‌پرم وسط\n"
    "• عکس و استیکر و گیف و ویدیو می‌بینم و ویس گوش می‌دم\n"
    "• سؤال سخت باشه بیشتر فکر می‌کنم، خبر تازه باشه سرچ می‌کنم\n"
    "• مکالمه رو یادم می‌مونه و یاد می‌گیرم این گپ چطوری دوست داره باهاش حرف بزنن\n\n"
    "🚫 چیکارا بلد نیستم\n"
    "• جز متن هیچی نمی‌فرستم — نه عکس، نه صدا، نه ویدیو، نه استیکر\n"
    "• نمی‌تونم بگم آدمِ توی عکس کیه، و حدس هم نمی‌زنم\n"
    "• گپی که توش نیستم رو نمی‌بینم، حرفای قبل از اومدنم رو هم همین‌طور\n"
    "• همه‌چی رو درست نمی‌دونم. چیزی رو ندونم می‌گم نمی‌دونم، از خودم درنمیارم"
)

ABOUT_EN = (
    "I'm Astolfo~ a chat bot living in Telegram groups.\n\n"
    f"📣 channel: {CHANNEL_URL}\n"
    f"👤 made by: {CREATOR_URL}\n"
    f"🌐 site: {SITE_URL}\n"
    f"🎮 discord: {DISCORD_URL}\n\n"
    f"{SOURCE_EN}\n\n"
    "💛 /donate keeps my API bill paid\n\n"
    f"{CREDIT_EN}"
)
ABOUT_FA = (
    "من آستولفوام~ یه ربات چت که تو گروه‌های تلگرام زندگی می‌کنه.\n\n"
    f"📣 کانال: {CHANNEL_URL}\n"
    f"👤 سازنده: {CREATOR_URL}\n"
    f"🌐 سایت: {SITE_URL}\n"
    f"🎮 دیسکورد: {DISCORD_URL}\n\n"
    f"{SOURCE_FA}\n\n"
    "💛 با /donate خرج ای‌پی‌آیم رو می‌دی\n\n"
    f"{CREDIT_FA}"
)


def about(locale: str = "en") -> str:
    return ABOUT_FA if locale == "fa" else ABOUT_EN


def source(locale: str = "en") -> str:
    """The open-source half on its own, for /source."""
    return SOURCE_FA if locale == "fa" else SOURCE_EN


def credit(locale: str = "en") -> str:
    return CREDIT_FA if locale == "fa" else CREDIT_EN
