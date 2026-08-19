"""What the bot can answer with no model behind it.

Only things that need no knowledge: a greeting, a thank-you, what it is, the time,
a sum. Everything else gets an honest "my brain is offline", because a bot that
guesses when it cannot check is worse than one that says so.

Nothing here invents a fact. If a rule is not sure, it declines.
"""

from __future__ import annotations

import ast
import logging
import operator
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# -- what a message looks like -------------------------------------------
GREETING = re.compile(
    r"^\s*(hi|hey+|hello|yo|salam|salaam|سلام|درود|های|هلو|hii+)\b[\s!.~؟?]*$", re.I
)
FAREWELL = re.compile(
    r"^\s*(bye+|goodbye|good\s*night|شب\s*بخیر|خداحافظ|فعلا|بای)\b[\s!.~؟?]*$", re.I
)
THANKS = re.compile(
    r"^\s*(thanks|thank\s*you|thx|ty|ممنون|مرسی|دمت\s*گرم|تشکر)\b[\s!.~؟?]*$", re.I
)
HOW_ARE_YOU = re.compile(
    r"(how\s+are\s+you|how'?s\s+it\s+going|چطوری|خوبی|حالت\s*چطوره)", re.I
)
WHO_ARE_YOU = re.compile(
    r"(who\s+are\s+you|what\s+are\s+you|are\s+you\s+(a\s+)?(bot|human|ai)"
    r"|تو\s*کی(\s|‌)*ای|شما\s*کی(\s|‌)*ای|تو\s*چی\s*هستی|رباتی)",
    re.I,
)
TIME_QUESTION = re.compile(
    r"(what\s+time|the\s+time\s+now|ساعت\s*چند|چه\s*ساعتی)", re.I
)
DATE_QUESTION = re.compile(
    r"(what\s+(is\s+)?the\s+date|what\s+day\s+is|چه\s*تاریخی|امروز\s*چندم)", re.I
)
PING = re.compile(r"^\s*(ping|test|تست)\s*[!.?؟]*$", re.I)

# People address the bot before asking, and "astolfo 2+2" is still a sum.
ADDRESS = re.compile(
    r"(^\s*(@?\w*astolfo\w*|آستولفو|استولفو)\s*[،,:!ـ-]*\s*)"
    r"|(\s*(@?\w*astolfo\w*|آستولفو|استولفو)\s*[!.?؟]*\s*$)",
    re.I,
)

# A sum, not an expression language: digits and the four operators only.
SUM = re.compile(r"^[\s\d+\-*/×÷().,]+$")
_ARITHMETIC = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

REPLIES: dict[str, dict[str, list[str]]] = {
    "greeting": {
        "en": ["yahoo~ hi! 👋", "hey hey~ 🌸", "oh, hi! 😄"],
        "fa": ["یاهو~ سلام! 👋", "به‌به سلام 🌸", "سلااام 😄"],
    },
    "farewell": {
        "en": ["byeee~ 👋", "see you! 🌙", "night night~ 😴"],
        "fa": ["بایـی~ 👋", "می‌بینمت! 🌙", "شب خوش~ 😴"],
    },
    "thanks": {
        "en": ["anytime~ 😌", "ehehe, of course 💛", "no problem at all!"],
        "fa": ["قابلی نداشت~ 😌", "خواهش! 💛", "کاری نکردم که!"],
    },
    "how_are_you": {
        "en": ["bouncing off the walls as usual~ you? 😄", "great! a bit bored, honestly 😌"],
        "fa": ["مثل همیشه پرانرژی~ تو چطوری؟ 😄", "خوبم! یکم حوصله‌م سر رفته 😌"],
    },
    "ping": {"en": ["pong~ 🏓 I'm here!"], "fa": ["پونگ~ 🏓 اینجام!"]},
}

WHO_AM_I = {
    "en": "I'm Astolfo~ a chat bot living in this group 🌸",
    "fa": "من آستولفوام~ یه ربات چت که تو این گروه زندگی می‌کنه 🌸",
}

NO_BRAIN = {
    "en": (
        "my brain is offline right now 😵‍💫 every model I can reach is out of "
        "allowance. I can still say hi, tell you the time and do sums~ ask me "
        "properly again in a bit!"
    ),
    "fa": (
        "مغزم فعلاً آفلاینه 😵‍💫 همه‌ی مدل‌هایی که دارم سهمیه‌شون تموم شده. هنوز "
        "می‌تونم سلام کنم، ساعت بگم و حساب کتاب کنم~ یکم بعد دوباره بپرس!"
    ),
}


def _pick(kind: str, locale: str, seed: int) -> str:
    """Vary the wording without randomness, so the same message reads the same."""
    options = REPLIES[kind].get(locale) or REPLIES[kind]["en"]
    return options[seed % len(options)]


def _arithmetic(node: ast.AST) -> float:
    """Evaluate a parsed sum. Only numbers and the four operators, nothing else."""
    if isinstance(node, ast.Expression):
        return _arithmetic(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITHMETIC:
        return _ARITHMETIC[type(node.op)](_arithmetic(node.left), _arithmetic(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITHMETIC:
        return _ARITHMETIC[type(node.op)](_arithmetic(node.operand))
    raise ValueError("not a sum")


def calculate(text: str) -> str | None:
    """The answer to a plain sum, or None when it is not one."""
    cleaned = text.strip().rstrip("=؟?").strip()
    if not cleaned or not SUM.match(cleaned) or not any(c.isdigit() for c in cleaned):
        return None
    if not any(op in cleaned for op in "+-*/×÷"):
        return None

    expression = cleaned.replace("×", "*").replace("÷", "/").replace(",", "")
    try:
        value = _arithmetic(ast.parse(expression, mode="eval"))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, RecursionError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN or overflow
        return None
    return str(int(value)) if float(value).is_integer() else f"{value:.6g}"


def strip_address(text: str) -> str:
    """Drop the name it was called by, so what is left is the actual message."""
    previous = None
    body = (text or "").strip()
    while body != previous:
        previous = body
        body = ADDRESS.sub("", body).strip()
    return body


def answer(text: str, *, locale: str = "en", bot_name: str = "Astolfo") -> str | None:
    """A reply that needs no model, or None when the question needs one."""
    called = (text or "").strip()
    if not called:
        return None
    body = strip_address(called)
    if not body:
        # Nothing left but the name it was called by, which is somebody saying hi.
        return _pick("greeting", locale, len(called))
    seed = len(body)

    if GREETING.match(body):
        return _pick("greeting", locale, seed)
    if FAREWELL.match(body):
        return _pick("farewell", locale, seed)
    if THANKS.match(body):
        return _pick("thanks", locale, seed)
    if PING.match(body):
        return _pick("ping", locale, seed)
    if HOW_ARE_YOU.search(body):
        return _pick("how_are_you", locale, seed)
    if WHO_ARE_YOU.search(body):
        return WHO_AM_I.get(locale, WHO_AM_I["en"])

    now = datetime.now(timezone.utc)
    if TIME_QUESTION.search(body):
        clock = now.strftime("%H:%M")
        return (
            f"ساعت {clock} به وقت گرینویچه~ ⏰" if locale == "fa" else f"it's {clock} UTC~ ⏰"
        )
    if DATE_QUESTION.search(body):
        today = now.strftime("%A, %d %B %Y")
        return f"امروز {today} ـه (میلادی)~ 📅" if locale == "fa" else f"today is {today}~ 📅"

    total = calculate(body)
    if total is not None:
        return f"{total} ✨"

    return None


def excuse(locale: str = "en") -> str:
    """What to say when the question genuinely needed the model that is missing."""
    return NO_BRAIN.get(locale, NO_BRAIN["en"])
