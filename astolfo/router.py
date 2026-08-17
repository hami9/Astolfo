"""مسیریاب هوشمند پاسخ: تصمیم بین «سریع»، «تفکر»، «جست‌وجو» و «جدی».

مرحلهٔ اول قواعد سریع (بدون هزینه و بدون تأخیر) است؛ فقط وقتی نتیجه قطعی نبود
یک فراخوانی کوچک به مدل سبک انجام می‌شود. این‌طوری گپ‌وگفت معمولی فوری جواب
می‌گیرد و سؤال‌های واقعی، فکر و منبع.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from .ai import AIClient
from .config import Settings

log = logging.getLogger("astolfo.router")

FAST = "fast"
THINK = "think"
SEARCH = "search"
SERIOUS = "serious"
VALID_MODES = {FAST, THINK, SEARCH, SERIOUS}


@dataclass
class Decision:
    mode: str = FAST
    web: bool = False
    source: str = "heuristic"
    reason: str = ""
    query: Optional[str] = None

    def __str__(self) -> str:  # برای لاگ
        return f"{self.mode}{'+web' if self.web else ''} ({self.source}: {self.reason})"


# --------------------------------------------------------------------------
# الگوها
# --------------------------------------------------------------------------
_FORCE_SEARCH = re.compile(
    r"(سرچ\s*کن|جست\s*و?\s*جو\s*کن|گوگل\s*کن|بگرد|منبع\s*بده|لینک\s*بده|"
    r"\bsearch\b|\bgoogle\b|\blook\s+it\s+up\b)",
    re.I,
)
_FORCE_THINK = re.compile(
    r"(فکر\s*کن|دقیق\s*(باش|بگو)|با\s*دقت|تحلیل\s*کن|\bthink\b|\bcarefully\b)", re.I
)
_FORCE_FAST = re.compile(r"(سریع\s*بگو|کوتاه\s*بگو|\bquick(ly)?\b|\btldr\b)", re.I)

_FRESHNESS = re.compile(
    r"(امروز|دیروز|امسال|الان|همین\s*الان|فعلاً|جدیدترین|آخرین|تازه‌?ترین|اخیر|"
    r"خبر|اخبار|قیمت|نرخ|دلار|سکه|بورس|ارز|بیت\s*کوین|آب\s*و\s*هوا|هوا\s*چطوره|"
    r"نتیجهٔ?\s*بازی|چند\s*شد|کی\s*برد|منتشر\s*شد|آپدیت|نسخهٔ?\s*جدید|"
    r"\btoday\b|\blatest\b|\bcurrent(ly)?\b|\bnews\b|\bprice\b|\bweather\b|\bscore\b|"
    r"\brelease[ds]?\b|\bversion\b|\bnow\b)",
    re.I,
)
_FACT_LOOKUP = re.compile(
    r"(کیه|کیست|چیه\s*دقیقاً|چند\s*سالشه|متولد|تاریخ\s*تولد|چقدره|چند\s*تاست|"
    r"کجاست|کِی\s*(بود|شد)|چند\s*درصد|آمار|رکورد|"
    r"\bwho\s+is\b|\bwhen\s+(was|did)\b|\bhow\s+(much|many|old)\b|\bstatistic|\brecord\b)",
    re.I,
)
_URL = re.compile(r"https?://\S+")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")

_COMPLEX = re.compile(
    r"(چطور|چگونه|چرا|توضیح\s*بده|یاد\s*بده|فرق|تفاوت|مقایسه|مقایسه\s*کن|کدوم\s*بهتر|"
    r"بهتره|پیشنهاد|حل\s*کن|بنویس|کد|برنامه|خطا|ارور|باگ|دیباگ|ترجمه\s*کن|خلاصه\s*کن|"
    r"معنی|نظرت\s*چیه\s*درباره|"
    r"\bhow\s+(do|to|can|does)\b|\bwhy\b|\bexplain\b|\bcompare\b|\bdebug\b|\berror\b|"
    r"\bcode\b|\bwrite\s+(a|the|me)\b|\btranslate\b|\bsummar)",
    re.I,
)
_MATHY = re.compile(r"\d+\s*[\+\-\*/×÷^]\s*\d+|\bدرصد\b.*\d|\d+\s*%")

_SERIOUS = re.compile(
    r"(افسرده|افسردگی|خودکشی|خودم\s*رو\s*بکشم|نمی‌?خوام\s*زنده|حالم\s*(خیلی\s*)?بده|"
    r"داغونم|گریه\s*(کردم|می‌?کنم)|تنهام|بریدم|خسته‌?ام\s*از\s*زندگی|اضطراب|پنیک|"
    r"فوت\s*(کرد|شد)|مرد(ه)?\s*(بابام|مامانم|دوستم)|بیمارستان|سرطان|طلاق|بی‌?کار\s*شدم|"
    r"\bdepress|\bsuicid|\bkill\s+myself\b|\bpanic\s+attack\b|\bi\s+feel\s+(awful|terrible|empty)\b|"
    r"\bpassed\s+away\b|\bmy\s+(dad|mom|friend)\s+died\b)",
    re.I,
)

_CHATTER = re.compile(
    r"^(سلام|سلوم|درود|هی|های|چطوری|خوبی|خوبین|مرسی|ممنون|دمت\s*گرم|خدافظ|بای|"
    r"ای?ول|لول|خخخ|هه|اوکی|باشه|آره|نه|چه\s*باحال|جانم|هوی|"
    r"hi|hey|hello|yo|lol|ok|okay|thanks|thx|bye|nice|cool|wow)\b",
    re.I,
)


def _is_probably_question(text: str) -> bool:
    return bool(re.search(r"[?؟]", text))


# --------------------------------------------------------------------------
# مرحلهٔ ۱ — قواعد سریع
# --------------------------------------------------------------------------
def heuristic_decision(text: str, *, has_media: bool = False) -> tuple[Decision, float]:
    """تصمیم اولیه + میزان اطمینان (۰ تا ۱)."""
    body = (text or "").strip()
    words = body.split()

    if _SERIOUS.search(body):
        return Decision(SERIOUS, False, "heuristic", "نشانهٔ ناراحتی جدی"), 0.95

    if _FORCE_SEARCH.search(body):
        return Decision(SEARCH, True, "forced", "درخواست صریح جست‌وجو", body[:200]), 1.0
    if _FORCE_THINK.search(body):
        return Decision(THINK, False, "forced", "درخواست صریح تفکر"), 1.0
    if _FORCE_FAST.search(body):
        return Decision(FAST, False, "forced", "درخواست پاسخ کوتاه"), 1.0

    if not body and has_media:
        return Decision(FAST, False, "heuristic", "فقط رسانه بدون متن"), 0.9

    # گپ کوتاه و روزمره → همیشه سریع
    if len(words) <= 4 and not _is_probably_question(body):
        return Decision(FAST, False, "heuristic", "پیام کوتاه گپی"), 0.9
    if _CHATTER.match(body) and len(words) <= 8:
        return Decision(FAST, False, "heuristic", "احوال‌پرسی"), 0.88

    needs_fresh = bool(_FRESHNESS.search(body))
    fact_lookup = bool(_FACT_LOOKUP.search(body))
    has_url = bool(_URL.search(body))
    if needs_fresh and (_is_probably_question(body) or fact_lookup or has_url):
        return Decision(SEARCH, True, "heuristic", "نیاز به اطلاعات به‌روز", body[:200]), 0.85
    if fact_lookup and _YEAR.search(body):
        return Decision(SEARCH, True, "heuristic", "پرسش واقعیت‌محور تاریخ‌دار", body[:200]), 0.8

    if _COMPLEX.search(body) or _MATHY.search(body) or len(body) > 220:
        return Decision(THINK, False, "heuristic", "پرسش تحلیلی/فنی"), 0.75

    if _is_probably_question(body) and len(words) > 8:
        return Decision(THINK, False, "heuristic", "پرسش نسبتاً بلند"), 0.5

    return Decision(FAST, False, "heuristic", "پیش‌فرض گپ"), 0.55


# --------------------------------------------------------------------------
# مرحلهٔ ۲ — داور مدل سبک
# --------------------------------------------------------------------------
ROUTER_POLICY = """\
You are the internal dispatcher of a Telegram persona bot (a playful character who
chats in a group). You never talk to users. You only classify the newest message and
output JSON.

Modes:
- "fast"   : banter, greetings, jokes, opinions, reactions, roleplay, anything the bot
             can answer instantly from general knowledge without risk of being wrong.
- "think"  : needs real reasoning — coding, math, debugging, comparisons, planning,
             explanations, translation of tricky text, multi-step questions, advice
             where a wrong answer would matter.
- "search" : needs facts the bot cannot safely know from memory — current events,
             prices, weather, sports results, releases/versions, dates, statistics,
             specific real people/companies/products, anything time-sensitive or
             easily hallucinated, or a URL the user wants read.
- "serious": the user is genuinely upset, grieving, scared, or in distress. Emotional
             support, not information.

Rules:
- Prefer "fast". Only escalate when it actually changes answer quality.
- Choose "search" whenever being wrong is likely and verifiable — avoiding invented
  facts matters more than speed.
- "web" must be true for "search", and may be true with "think" when the reasoning
  also depends on outside facts.
- "query" is a short, self-contained web query in the language most likely to find the
  answer (empty string when web is false).

Answer ONLY with JSON:
{"mode":"fast|think|search|serious","web":true|false,"query":"","why":"max 6 words"}"""


async def llm_decision(
    ai: AIClient,
    settings: Settings,
    *,
    text: str,
    recent: List[str],
    has_media: bool,
) -> Optional[Decision]:
    context = "\n".join(recent[-4:]) if recent else "(no earlier messages)"
    user_block = (
        f"Recent chat:\n{context}\n\n"
        f"Newest message{' (has attached media)' if has_media else ''}:\n{text[:800]}"
    )
    data = await ai.json_call(
        [
            {"role": "system", "content": ROUTER_POLICY},
            {"role": "user", "content": user_block},
        ],
        model=settings.model_router,
        max_tokens=settings.router_max_tokens,
    )
    if not data:
        return None

    mode = str(data.get("mode", "")).lower().strip()
    if mode not in VALID_MODES:
        return None
    web = bool(data.get("web")) or mode == SEARCH
    query = (data.get("query") or "").strip() or None
    why = str(data.get("why") or "")[:60]
    return Decision(mode, web, "llm", why or "تصمیم مدل", query)


# --------------------------------------------------------------------------
# API اصلی
# --------------------------------------------------------------------------
async def decide(
    ai: AIClient,
    settings: Settings,
    *,
    text: str,
    recent: Optional[List[str]] = None,
    has_media: bool = False,
    forced_mode: Optional[str] = None,
) -> Decision:
    if forced_mode in VALID_MODES:
        return Decision(forced_mode, forced_mode == SEARCH, "user", "حالت دستی", text[:200])

    decision, confidence = heuristic_decision(text, has_media=has_media)

    # حالت جدی و درخواست صریح کاربر نیازی به داوری دوم ندارند
    if confidence >= 0.85 or not settings.router_llm_enabled:
        return decision

    llm = await llm_decision(ai, settings, text=text, recent=recent or [], has_media=has_media)
    if llm is None:
        return decision

    # اگر قاعده‌ها «جدی» تشخیص داده بودند، مدل نمی‌تواند آن را پایین بیاورد
    if decision.mode == SERIOUS:
        return decision
    if not settings.web_search_enabled and llm.mode == SEARCH:
        llm.mode = THINK
        llm.web = False
    return llm
