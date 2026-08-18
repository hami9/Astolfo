"""User-facing command strings, in English and Persian."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

EN: dict[str, str] = {
    "greeting": (
        "Yahoo! 👋 I'm Astolfo, legendary rider, strongest... okay fine, weakest paladin, "
        "but definitely the cutest one~\n"
        "I'm part of this chat now! Reply to me or mention me and I'll always answer, and "
        "the rest of the time I just jump into conversations because I can't stay quiet 😌\n"
        "Send photos, voice messages, GIFs and videos too, I'll look and tell you what I think!"
    ),
    "help": (
        "Astolfo here~ this is what I do:\n\n"
        "• Reply to me or mention me and I always answer, otherwise I jump in sometimes\n"
        "• I look at photos, stickers, GIFs, videos and listen to voice messages\n"
        "• Hard question, I think about it. Fresh news, I search. Small talk, instant answer\n"
        "• I only send text, I can't generate images or audio\n\n"
        "Commands:\n"
        "/chance 0-100 — how often I join in on my own\n"
        "/mode auto|fast|think|search — how I answer\n"
        "/usage — what I've spent today\n"
        "/status — current settings\n"
        "/reset — forget this chat's history and notes\n"
        "/mute and /unmute — quiet me down or bring me back"
    ),
    "admin_only": "Only admins can change that one~",
    "reset_done": "Hop! Everything just fell out of my head 🧹 (not a hard task, honestly)",
    "chance_current": "Right now I jump in {percent}% of the time. Change it with /chance 40.",
    "chance_set": "Okay! From now on I'll jump in {percent}% of the time 😌",
    "chance_bad": "Give me a number from 0 to 100, like /chance 40",
    "mode_current": (
        "Current mode: {mode}\n"
        "auto = I decide, fast = instant, think = careful, search = with web results"
    ),
    "mode_set": "Mode is {mode} now ✨",
    "mode_bad": "Pick one of these: auto / fast / think / search",
    "muted": "Okay okay, going quiet 🤐 (bring me back with /unmute)",
    "unmuted": "I'm baaack! 🎉 You missed me, right?",
    "error_reply": "Ugh... my brain flew to the Moon and didn't come back 😵‍💫 say that again?",
    "budget_stopped": "I've burned through today's allowance~ back tomorrow! 💸",
    "no_credit": "my magic ran out of credit 😭 someone needs to top up the API account!",
    "status": (
        "My status:\n"
        "• mode: {mode}   • muted: {muted}\n"
        "• auto-join chance: {chance}%\n"
        "• messages in memory: {history}/{max_history}\n"
        "• long-term notes: {notes}\n"
        "• replies here: {replies}\n"
        "• mode: {billing}\n"
        "• fast model: {model_fast}\n"
        "• think model: {model_think}\n"
        "• web search: {web}\n"
        "• voice/video analysis: {ffmpeg}"
    ),
    "usage": (
        "Credit usage:\n"
        "• today: ${cost_today} {budget_note}\n"
        "• this month: ${cost_month}\n"
        "• model calls today: {calls}\n"
        "• tokens: {prompt_tokens} in / {completion_tokens} out\n"
        "• prompt cache hits: {cache_hit_rate} ({cached_tokens} tokens reused)\n"
        "• replies served from cache: {cache_replies}\n"
        "• router shortcuts: {router_saved}\n"
        "• cost by mode: {by_mode}\n"
        "• state: {level}"
    ),
}

FA: dict[str, str] = {
    "greeting": (
        "یاهووو! 👋 من آستولفوام، سوارکار افسانه‌ای، قوی‌ترین... باشه باشه، ضعیف‌ترین پالادین، "
        "ولی صددرصد کیوت‌ترینشون~\n"
        "از این به بعد منم عضو این گروهم! ریپلای یا منشنم کنی حتماً جواب می‌دم، بقیهٔ وقت‌ها هم "
        "خودم می‌پرم وسط بحث چون خب... نمی‌تونم ساکت بمونم 😌\n"
        "عکس و ویس و گیف و ویدیو هم بفرست، نگاه می‌کنم و می‌گم نظرم چیه!"
    ),
    "help": (
        "منم آستولفو~ اینا کارهاییه که بلدم:\n\n"
        "• ریپلای یا منشنم کنی همیشه جواب می‌دم، وگرنه گاهی خودم می‌پرم وسط بحث\n"
        "• عکس، استیکر، گیف، ویدیو و ویس رو می‌بینم و گوش می‌دم\n"
        "• سؤال سخت باشه فکر می‌کنم، خبر روز باشه سرچ می‌کنم، گپ ساده باشه سریع جواب می‌دم\n"
        "• فقط متن می‌فرستم؛ عکس و صدا نمی‌سازم\n\n"
        "دستورها:\n"
        "/chance ۰تا۱۰۰ — چقدر خودم بپرم وسط بحث\n"
        "/mode auto|fast|think|search — حالت جواب دادن\n"
        "/usage — مصرف امروز\n"
        "/status — وضعیت فعلی\n"
        "/reset — پاک کردن حافظهٔ این چت\n"
        "/mute و /unmute — ساکتم کن / برم گردون"
    ),
    "admin_only": "این یکی رو فقط ادمین‌ها می‌تونن بزنن~",
    "reset_done": "هوپ! همه‌چی از ذهنم پرید 🧹 (که خب... کار سختی نبود)",
    "chance_current": "الان {percent}٪ مواقع خودم می‌پرم وسط بحث. با /chance 40 عوضش کن.",
    "chance_set": "باشه! از این به بعد {percent}٪ مواقع می‌پرم وسط 😌",
    "chance_bad": "یه عدد بین ۰ تا ۱۰۰ بده. مثلاً: /chance 40",
    "mode_current": (
        "حالت فعلی: {mode}\n"
        "auto = خودم تصمیم می‌گیرم، fast = سریع، think = با فکر، search = با سرچ"
    ),
    "mode_set": "حالت شد {mode} ✨",
    "mode_bad": "یکی از اینا: auto / fast / think / search",
    "muted": "باشه باشه ساکت شدم 🤐 (با /unmute برم گردون)",
    "unmuted": "برگشتممم! 🎉 دلتون تنگ شده بود نه؟",
    "error_reply": "اوه... مغزم یه لحظه رفت رو ماه و برنگشت 😵‍💫 یه بار دیگه بگو؟",
    "budget_stopped": "سهمیهٔ امروزم تموم شد~ فردا برمی‌گردم! 💸",
    "no_credit": "کردیت جادوم ته کشید 😭 یکی حساب OpenRouter رو شارژ کنه!",
    "status": (
        "وضعیت من:\n"
        "• حالت: {mode}   • ساکت: {muted}\n"
        "• احتمال ورود خودکار: {chance}٪\n"
        "• پیام‌های تو حافظه: {history}/{max_history}\n"
        "• یادداشت بلندمدت: {notes}\n"
        "• جواب‌هایی که اینجا دادم: {replies}\n"
        "• حالت: {billing}\n"
        "• مدل سریع: {model_fast}\n"
        "• مدل فکری: {model_think}\n"
        "• سرچ وب: {web}\n"
        "• تحلیل ویس/ویدیو: {ffmpeg}"
    ),
    "usage": (
        "مصرف کردیت:\n"
        "• امروز: ${cost_today} {budget_note}\n"
        "• این ماه: ${cost_month}\n"
        "• تعداد فراخوانی مدل امروز: {calls}\n"
        "• توکن: {prompt_tokens} ورودی / {completion_tokens} خروجی\n"
        "• کش پرامپت: {cache_hit_rate} ({cached_tokens} توکن دوباره استفاده شد)\n"
        "• جواب‌های از کش: {cache_replies}\n"
        "• میان‌بُرهای مسیریاب: {router_saved}\n"
        "• هزینه بر اساس حالت: {by_mode}\n"
        "• وضعیت: {level}"
    ),
}

TABLES = {"en": EN, "fa": FA}


def normalize_locale(raw: str | None) -> str:
    """Accept sloppy values like "fa en", " FA " or "fa_IR" and warn on the rest."""
    token = (raw or "").strip().lower().replace("_", "-").split()
    candidate = token[0].split("-")[0] if token else ""
    if candidate in TABLES:
        return candidate
    if raw and raw.strip():
        log.warning("BOT_LANG=%r is not a supported language, falling back to en", raw)
    return "en"


class Strings:
    def __init__(self, locale: str = "en") -> None:
        self.locale = normalize_locale(locale)
        self._table = TABLES[self.locale]

    def __call__(self, key: str, **kwargs) -> str:
        template = self._table.get(key) or EN.get(key, key)
        return template.format(**kwargs) if kwargs else template
