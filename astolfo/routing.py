"""Response routing: pick fast, think, search or serious for each message.

Cheap regex heuristics run first and settle most turns for free. A small LLM
dispatcher is consulted only when the heuristics are not confident, and its verdicts
are cached so repeated phrasing costs nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .cache import TTLCache, normalize
from .config import Settings
from .llm import LLMClient, Usage
from .persona import FAST, SEARCH, SERIOUS, THINK

log = logging.getLogger(__name__)

MODES = {FAST, THINK, SEARCH, SERIOUS}
CONFIDENT = 0.85


@dataclass(frozen=True)
class Decision:
    mode: str = FAST
    web: bool = False
    source: str = "heuristic"
    reason: str = ""
    query: str | None = None

    def __str__(self) -> str:
        return f"{self.mode}{'+web' if self.web else ''} ({self.source}: {self.reason})"


# Explicit user instructions win over everything else.
_FORCE_SEARCH = re.compile(
    r"(سرچ\s*کن|جست\s*و?\s*جو\s*کن|گوگل\s*کن|بگرد|منبع\s*بده|لینک\s*بده"
    r"|\bsearch\b|\bgoogle\b|\blook\s+it\s+up\b)",
    re.I,
)
_FORCE_THINK = re.compile(
    r"(فکر\s*کن|دقیق\s*(باش|بگو)|با\s*دقت|تحلیل\s*کن|\bthink\b|\bcarefully\b)", re.I
)
_FORCE_FAST = re.compile(r"(سریع\s*بگو|کوتاه\s*بگو|\bquick(ly)?\b|\btl;?dr\b)", re.I)

# Anything time-sensitive or easily hallucinated goes to search.
_FRESHNESS = re.compile(
    r"(امروز|دیروز|امسال|الان|فعلاً|جدیدترین|آخرین|تازه‌?ترین|اخیر|خبر|اخبار|قیمت|نرخ"
    r"|دلار|سکه|بورس|ارز|بیت\s*کوین|آب\s*و\s*هوا|هوا\s*چطوره|نتیجهٔ?\s*بازی|چند\s*شد"
    r"|کی\s*برد|منتشر\s*شد|آپدیت|نسخهٔ?\s*جدید"
    r"|\btoday\b|\blatest\b|\bcurrent(ly)?\b|\bnews\b|\bprice\b|\bweather\b|\bscore\b"
    r"|\breleased?\b|\bversion\b|\bright\s+now\b)",
    re.I,
)
_FACT_LOOKUP = re.compile(
    r"(کیه|کیست|چند\s*سالشه|متولد|تاریخ\s*تولد|چقدره|چند\s*تاست|کجاست|چند\s*درصد|آمار|رکورد"
    r"|\bwho\s+is\b|\bwhen\s+(was|did)\b|\bhow\s+(much|many|old)\b|\bstatistics?\b|\brecord\b)",
    re.I,
)
_URL = re.compile(r"https?://\S+")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")

# Reasoning-heavy requests.
_COMPLEX = re.compile(
    r"(چطور|چگونه|چرا|توضیح\s*بده|یاد\s*بده|فرق|تفاوت|مقایسه|کدوم\s*بهتر|بهتره|پیشنهاد"
    r"|حل\s*کن|بنویس|کد|برنامه|خطا|ارور|باگ|دیباگ|ترجمه\s*کن|خلاصه\s*کن|معنی"
    r"|\bhow\s+(do|to|can|does)\b|\bwhy\b|\bexplain\b|\bcompare\b|\bdebug\b|\berror\b"
    r"|\bcode\b|\bwrite\s+(a|the|me)\b|\btranslate\b|\bsummar)",
    re.I,
)
_MATH = re.compile(r"\d+\s*[+\-*/×÷^]\s*\d+|\d+\s*%")

# Work somebody wants done rather than a conversation. Unless the owner has asked
# for a solver, escalating these is paying think-model prices for an answer the
# persona is going to decline anyway.
_HEAVY = re.compile(
    r"(تکلیف|تمرین|پروژه|پایان\s*نامه|مقاله|انشا|رساله|حلش\s*کن|حل\s*کن.*(معادله|انتگرال|مشتق)"
    r"|انتگرال|مشتق|ماتریس|معادله\s*دیفرانسیل|اثبات\s*کن"
    r"|برنامه\s*(بنویس|رو\s*بنویس)|کد\s*(کامل|رو\s*بنویس)|اپ\s*بساز|سایت\s*بساز|ربات\s*بساز"
    r"|\bhomework\b|\bassignment\b|\bessay\b|\bthesis\b|\bdissertation\b"
    r"|\bintegral\b|\bderivative\b|\bmatri(x|ces)\b|\bprove\s+that\b|\btheorem\b"
    # "write me a bot", and "write me a python bot" with a word in between.
    r"|\b(write|build|make|code)\s+(me\s+)?(a|an|the)\s+(\w+\s+){0,2}"
    r"(program|app|bot|chatbot|script|website|site|game|api|server)\b)",
    re.I,
)

# Emotional distress.
_SERIOUS = re.compile(
    r"(افسرده|افسردگی|خودکشی|خودم\s*رو\s*بکشم|نمی‌?خوام\s*زنده|حالم\s*(خیلی\s*)?بده|داغونم"
    r"|گریه\s*(کردم|می‌?کنم)|تنهام|بریدم|خسته‌?ام\s*از\s*زندگی|اضطراب|پنیک|فوت\s*(کرد|شد)"
    r"|بیمارستان|سرطان|طلاق"
    r"|\bdepress|\bsuicid|\bkill\s+myself\b|\bpanic\s+attack\b|\bpassed\s+away\b"
    r"|\bi\s+feel\s+(awful|terrible|empty)\b)",
    re.I,
)

_CHATTER = re.compile(
    r"^(سلام|سلوم|درود|هی|های|چطوری|خوبی|خوبین|مرسی|ممنون|دمت\s*گرم|خدافظ|بای|ایول|لول"
    r"|خخخ|هه|اوکی|باشه|آره|نه|جانم|هوی"
    r"|hi|hey|hello|yo|lol|ok|okay|thanks|thx|bye|nice|cool|wow)\b",
    re.I,
)
_QUESTION = re.compile(r"[?؟]")


def heuristic(
    text: str, *, has_media: bool = False, heavy_lifting: bool = True
) -> tuple[Decision, float]:
    """Return a decision and a confidence score in [0, 1]."""
    body = (text or "").strip()
    words = body.split()

    if _SERIOUS.search(body):
        return Decision(SERIOUS, reason="distress signals"), 0.95
    if not heavy_lifting and _HEAVY.search(body):
        # It is going to decline this in character, so do not pay a think model to
        # produce the refusal.
        return Decision(FAST, reason="not what it is for"), 0.9
    if _FORCE_SEARCH.search(body):
        return Decision(SEARCH, True, "forced", "explicit search request", body[:200]), 1.0
    if _FORCE_THINK.search(body):
        return Decision(THINK, reason="explicit think request", source="forced"), 1.0
    if _FORCE_FAST.search(body):
        return Decision(FAST, reason="explicit short request", source="forced"), 1.0

    if not body and has_media:
        return Decision(FAST, reason="media without text"), 0.9
    if len(words) <= 4 and not _QUESTION.search(body):
        return Decision(FAST, reason="short chatter"), 0.9
    if _CHATTER.match(body) and len(words) <= 8:
        return Decision(FAST, reason="greeting"), 0.88

    fresh = bool(_FRESHNESS.search(body))
    lookup = bool(_FACT_LOOKUP.search(body))
    if fresh and (_QUESTION.search(body) or lookup or _URL.search(body)):
        return Decision(SEARCH, True, reason="time-sensitive question", query=body[:200]), 0.85
    if lookup and _YEAR.search(body):
        return Decision(SEARCH, True, reason="dated fact lookup", query=body[:200]), 0.8

    if _COMPLEX.search(body) or _MATH.search(body) or len(body) > 220:
        return Decision(THINK, reason="analytical request"), 0.75
    if _QUESTION.search(body) and len(words) > 8:
        return Decision(THINK, reason="long question"), 0.5
    return Decision(FAST, reason="default chatter"), 0.55


DISPATCHER_PROMPT = """\
You are the internal dispatcher of a Telegram persona bot. You never talk to users;
you classify the newest message and output JSON only.

Modes:
- "fast": banter, greetings, jokes, opinions, reactions, roleplay, anything answerable
  instantly from general knowledge with no real risk of being wrong.
- "think": needs real reasoning - coding, math, debugging, comparisons, planning,
  explanations, tricky translation, multi-step questions, advice where being wrong
  would matter.
- "search": needs facts the bot cannot safely know - current events, prices, weather,
  sports results, releases and versions, dates, statistics, specific real people,
  companies or products, anything time-sensitive or easily hallucinated, or a URL the
  user wants read.
- "serious": someone is sincerely sharing their own pain - grief, fear, despair,
  something frightening happening to them in real life. This mode is rare.
  It is NOT for: insults or threats aimed at the bot, people trash-talking each
  other, dark jokes, "kill yourself" or "go die" thrown around as banter, or
  roleplay violence. Those are "fast". Judge whether a real person is really
  hurting, not whether the words sound dark. When in doubt it is "fast".

Rules:
- Prefer "fast". Escalate only when it actually changes answer quality, because
  escalating costs money. Group chats are mostly banter, so "fast" is the common
  answer and the other three modes are exceptions.
- Choose "search" whenever being wrong is likely and verifiable; avoiding invented
  facts matters more than speed.
- "web" must be true for "search" and may be true for "think" when the reasoning
  depends on outside facts.
- "query" is a short self-contained web query in the language most likely to find the
  answer, or an empty string when web is false.

Reply with JSON only:
{"mode":"fast|think|search|serious","web":true|false,"query":"","why":"max 6 words"}"""


class Router:
    def __init__(self, settings: Settings, llm: LLMClient) -> None:
        self._s = settings
        self._llm = llm
        self.cache: TTLCache[Decision] = TTLCache(maxsize=1024, ttl=settings.router_cache_ttl)
        self.llm_calls = 0

    def configure(self, settings: Settings, llm: LLMClient) -> None:
        """Adopt reloaded settings, keeping the decisions already cached."""
        self._s = settings
        self._llm = llm

    async def decide(
        self,
        *,
        text: str,
        recent: list[str] | None = None,
        has_media: bool = False,
        forced_mode: str | None = None,
        forced_source: str = "user",
        allow_llm: bool = True,
    ) -> tuple[Decision, Usage]:
        if forced_mode in MODES:
            # A cost-driven downgrade must never turn a distress signal into banter.
            if forced_source != "user":
                fallback, _ = heuristic(
                    text, has_media=has_media, heavy_lifting=self._s.heavy_lifting
                )
                if fallback.mode == SERIOUS:
                    return fallback, Usage()
            manual = Decision(
                forced_mode, forced_mode == SEARCH, forced_source, "pinned mode", text[:200]
            )
            return manual, Usage()

        decision, confidence = heuristic(
            text, has_media=has_media, heavy_lifting=self._s.heavy_lifting
        )
        if confidence >= CONFIDENT or not self._s.router_llm or not allow_llm:
            return decision, Usage()
        if len(text.split()) < self._s.router_min_words:
            return decision, Usage()

        key = normalize(text)
        cached = self.cache.get(key)
        if cached is not None:
            return cached, Usage()

        verdict, usage = await self._ask_model(text, recent or [], has_media)
        if verdict is None:
            return decision, usage
        if decision.mode == SERIOUS:  # never downgrade a distress signal
            return decision, usage
        if not self._s.web_search and verdict.mode == SEARCH:
            verdict = Decision(THINK, False, verdict.source, verdict.reason)

        self.cache.set(key, verdict)
        return verdict, usage

    async def _ask_model(
        self, text: str, recent: list[str], has_media: bool
    ) -> tuple[Decision | None, Usage]:
        context = "\n".join(recent[-4:]) or "(no earlier messages)"
        payload = (
            f"Recent chat:\n{context}\n\n"
            f"Newest message{' (has attached media)' if has_media else ''}:\n{text[:800]}"
        )
        self.llm_calls += 1
        data, usage = await self._llm.json_chat(
            [
                {"role": "system", "content": DISPATCHER_PROMPT},
                {"role": "user", "content": payload},
            ],
            model=self._s.model_router,
            max_tokens=self._s.router_max_tokens,
        )
        if not data:
            return None, usage

        mode = str(data.get("mode", "")).lower().strip()
        if mode not in MODES:
            return None, usage
        return (
            Decision(
                mode=mode,
                web=bool(data.get("web")) or mode == SEARCH,
                source="llm",
                reason=str(data.get("why") or "model verdict")[:60],
                query=(data.get("query") or "").strip() or None,
            ),
            usage,
        )
