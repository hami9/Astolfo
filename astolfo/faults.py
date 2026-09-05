"""What a service's refusal actually said, in that service's own dialect.

Every one of these thirteen services says "no" differently, and until now the
answer was flattened into one line: `HTTP 429: cohere limit`. The body, which is
where the service says *which* limit and *how long*, was thrown away. So a trial
key hitting its twenty-calls-a-minute ceiling and an account that has spent its
monthly credit produced the same sentence, and the bot rested the same sixty
seconds for both - right for one, useless for the other.

This reads the body the way the service wrote it and answers three questions:

* **kind** - is this a rate limit, an exhausted quota, an empty wallet, a bad
  key, a block, or a rejected request?
* **scope** - per minute, per day, per month, or for this one request?
* **how long** - what the service asked for, or what the scope implies.

Nothing here calls anything. Given a response it returns a record, and the
record's `summary` is what goes in the log and on the panel, so the line you
read is the same fact the code acted on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# What went wrong, coarsely. Ordered by how long it is worth waiting.
RATE = "rate"  # too fast; seconds to minutes
QUOTA = "quota"  # an allowance is spent; hours to a month
CREDIT = "credit"  # the wallet is empty; nothing but a top-up fixes it
AUTH = "auth"  # the key was refused
BLOCKED = "blocked"  # the request reached an edge and never got to the service
REJECTED = "rejected"  # the service understood and said no to this request
SERVER = "server"  # their side broke
UNKNOWN = "unknown"

# How wide the limit is. The difference between a minute and a month is the
# whole point of this module.
MINUTE = "minute"
HOUR = "hour"
DAY = "day"
MONTH = "month"
REQUEST = "request"
ACCOUNT = "account"

# What each scope is worth waiting when the service does not say. Deliberately
# short of the real window: coming back early costs one refused call, coming
# back late costs every reply until then.
WAITS: dict[str, float] = {
    MINUTE: 60.0,
    HOUR: 900.0,
    DAY: 3 * 3600.0,
    MONTH: 12 * 3600.0,
    ACCOUNT: 600.0,
    REQUEST: 0.0,
}

# A ceiling on what a service may ask for directly. A `retry-after` of a day is
# a fact worth recording, not an instruction worth obeying blindly.
MAX_RETRY_AFTER = 6 * 3600.0

# When the scope says nothing, what the kind is worth on its own. An empty wallet
# is the one that must not be retried on a short loop: no amount of waiting fills
# it, and every attempt is a wasted call.
BY_KIND: dict[str, float] = {
    CREDIT: 6 * 3600.0,
    QUOTA: 3 * 3600.0,
    RATE: 60.0,
    BLOCKED: 600.0,
}


# What each kind is, in words somebody reading the panel can act on. "auth" and
# "blocked" are the same status code and completely different problems, and the
# owner is the one who has to tell them apart.
SAYS: dict[str, str] = {
    RATE: "too many requests",
    QUOTA: "the allowance is spent",
    CREDIT: "out of credit",
    AUTH: "the key was refused",
    BLOCKED: "the request never reached the service",
    REJECTED: "the request was refused",
    SERVER: "the service is broken right now",
    UNKNOWN: "an unrecognised refusal",
}


@dataclass(frozen=True)
class Fault:
    """One refusal, read rather than guessed."""

    status: int
    service: str = ""
    kind: str = UNKNOWN
    scope: str = ""
    # What the service asked for, in seconds. 0 when it did not say.
    retry_after: float = 0.0
    # What it literally said, trimmed. Never invented, never paraphrased.
    said: str = ""
    model: str = ""

    @property
    def wait(self) -> float:
        """How long to rest this service, in seconds.

        The service's own number wins when it gave one, because it knows. The
        scope fills in when it did not.
        """
        if self.retry_after > 0:
            return min(self.retry_after, MAX_RETRY_AFTER)
        if self.kind == CREDIT:
            # Not a window that rolls: waiting is not what fixes this, so the
            # rest is long enough to stop the bot spending calls on being told.
            return BY_KIND[CREDIT]
        return WAITS.get(self.scope) or BY_KIND.get(self.kind, 0.0)

    @property
    def terminal(self) -> bool:
        """Whether waiting cannot fix this. Only a person can."""
        return self.kind in (CREDIT, AUTH)

    @property
    def window(self) -> str:
        """How wide the limit is, in words. Empty when the scope adds nothing."""
        if self.scope in (MINUTE, HOUR, DAY, MONTH):
            return f" per {self.scope}"
        if self.scope == ACCOUNT and self.kind in (RATE, QUOTA):
            return " for the whole account"
        return ""

    @property
    def summary(self) -> str:
        """One line, for the log and for the panel.

        What it is, how wide, how long, and then the service's own sentence -
        so the line somebody reads is the same fact the code acted on, and the
        quoted part is never something this module wrote.
        """
        where = self.service + (f"/{self.model}" if self.model else "")
        when = ""
        if self.retry_after > 0:
            when = f", retry in {self.retry_after:.0f}s"
        elif self.wait >= 60:
            when = f", resting {self.wait / 60:.0f}m"
        said = f' - "{self.said}"' if self.said else ""
        what = SAYS.get(self.kind, self.kind)
        return f"{where}: HTTP {self.status} {what}{self.window}{when}{said}"


def _text(body: object, depth: int = 0) -> str:
    """Every string a nested error object contains, flattened.

    Services bury the sentence at different depths - `error.message`,
    `error.detail[0].msg`, a bare `message`, a plain string - and rather than
    encoding each shape twice, once to find the text and once to read it, the
    text is gathered from wherever it is.
    """
    if depth > 4:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, (int, float)):
        return str(body)
    if isinstance(body, dict):
        return " ".join(_text(value, depth + 1) for value in body.values())
    if isinstance(body, list):
        return " ".join(_text(item, depth + 1) for item in body)
    return ""


def _seconds(text: str) -> float:
    """A wait a service wrote into its message: "6m30s", "in 31s", "59.2 s"."""
    match = re.search(r"(?:try again in|retry after|retry in|wait)\D{0,12}"
                      r"(?:(\d+)\s*m)?\s*(\d+(?:\.\d+)?)\s*s", text, re.I)
    if match:
        minutes = float(match.group(1) or 0)
        return minutes * 60 + float(match.group(2))
    match = re.search(r"(?:try again in|retry after|retry in|wait)\D{0,12}(\d+)\s*m", text, re.I)
    if match:
        return float(match.group(1)) * 60
    match = re.search(r'"?retryDelay"?[":\s]+(\d+(?:\.\d+)?)s', text, re.I)
    return float(match.group(1)) if match else 0.0


# The words each window is spelled with, longest first so "per day" does not
# match inside "requests per day per model" as something narrower.
_SCOPES: tuple[tuple[str, str], ...] = (
    (MONTH, r"per\s*month|monthly|per-month|permonth|this\s*month|/\s*month"),
    (DAY, r"per\s*day|daily|per-day|perday|/\s*day|rpd\b|free-models-per-day|per\s*24\s*h"),
    (HOUR, r"per\s*hour|hourly|per-hour|/\s*hour|rph\b"),
    (MINUTE, r"per\s*minute|per\s*min\b|/\s*min|rpm\b|tpm\b|perminute|per-minute"),
)


def _scope(text: str) -> str:
    for scope, pattern in _SCOPES:
        if re.search(pattern, text, re.I):
            return scope
    return ""


# An empty wallet, as each service words it. Deliberately specific: Google says
# "you exceeded your current quota, please check your plan and billing details"
# when a *free daily quota* runs out, and reading the word "billing" there turned
# an allowance that rolls at midnight into an empty account. What every real one
# of these has is a sentence about the balance itself, not about a plan.
_CREDIT = re.compile(
    r"insufficient\s+(credit|balance|funds)|out\s+of\s+credit|no\s+credits?\s+remaining"
    r"|negative\s+balance|positive\s+balance|depleted\s+your"
    r"|add\s+(more\s+)?(credit|balance|funds)|top\s*[- ]?up|payment\s+method"
    r"|payment\s+required|purchase\s+(pre[- ]?paid\s+)?credit",
    re.I,
)
_QUOTA = re.compile(
    r"quota|allowance|exceeded\s+your|included\s+credits|usage\s+limit|exhausted|credits?\s+"
    r"(have\s+)?(been\s+)?(used|spent)",
    re.I,
)
_RATE = re.compile(r"rate[\s_-]*limit|too\s+many\s+requests|slow\s+down|throttl", re.I)
_AUTH = re.compile(
    r"invalid\s+api\s*key|wrong\s+api\s*key|incorrect\s+api\s*key|unauthorized|unauthorised"
    r"|authentication|no\s+auth|api\s*key\s+not\s+(found|valid)|expired\s+token",
    re.I,
)


def _google_scope(body: dict) -> str:
    """Google names the exact quota it refused, which nobody else does.

    The violation carries an id like `GenerateRequestsPerMinutePerProjectPerModel`
    or `GenerateContentInputTokensPerModelPerDay`, and that id is the difference
    between waiting a minute and waiting until tomorrow.
    """
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            found = _scope(str(violation.get("quotaId") or violation.get("quotaMetric") or ""))
            if found:
                return found
    return ""


def _google_retry(body: dict) -> float:
    """Google puts the wait in a RetryInfo detail, as "31s".

    Read from the structure rather than from the flattened text: once the values
    are joined the "31s" has nothing next to it saying it is a delay, and the
    number would be as likely to be a token count.
    """
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    for detail in error.get("details") or []:
        if not isinstance(detail, dict) or "RetryInfo" not in str(detail.get("@type") or ""):
            continue
        match = re.match(r"(\d+(?:\.\d+)?)s$", str(detail.get("retryDelay") or "").strip())
        if match:
            return float(match.group(1))
    return 0.0


def read(
    status: int,
    body: str,
    *,
    service: str = "",
    model: str = "",
    retry_after: float = 0.0,
) -> Fault:
    """Read one refusal. Never raises: an unreadable body is still a fault."""
    try:
        parsed = json.loads(body) if body.strip().startswith(("{", "[")) else None
    except ValueError:
        parsed = None

    text = _text(parsed) if parsed is not None else (body or "")
    text = " ".join(text.split())[:400]

    scope, structured = "", 0.0
    if isinstance(parsed, dict):
        scope, structured = _google_scope(parsed), _google_retry(parsed)
    if not scope:
        scope = _scope(text)
    asked = retry_after or structured or _seconds(text)

    if status == 402:
        kind = CREDIT if not _QUOTA.search(text) or _CREDIT.search(text) else QUOTA
        scope = scope or (ACCOUNT if kind is CREDIT else "")
    elif status == 429:
        # The one that was being flattened. A trial key's twenty-a-minute and a
        # spent monthly allowance both arrive here, and they are not the same
        # wait - which is exactly what "limit, back in a minute, limit again"
        # looked like from the outside.
        if _CREDIT.search(text):
            kind = CREDIT
        elif scope in (MINUTE, HOUR):
            # A window that short is a rate limit by definition, whatever words
            # the service wrapped it in - Google calls its per-minute ceiling a
            # quota and says "resource exhausted", and it still rolls in a minute.
            kind = RATE
        elif scope in (DAY, MONTH) or (_QUOTA.search(text) and not _RATE.search(text)):
            kind = QUOTA
        else:
            kind = RATE
            scope = scope or MINUTE
    elif status == 401:
        kind = AUTH
    elif status == 403:
        # A 403 that never reached the service's auth layer is an edge block, not
        # a bad key. Cerebras returns exactly this from Cloudflare for some
        # datacentre ranges while the same key gets a clean 401 when it is wrong.
        kind = AUTH if _AUTH.search(text) else BLOCKED
        scope = scope or REQUEST
    elif status >= 500:
        kind, scope = SERVER, scope or REQUEST
    elif status >= 400:
        kind, scope = REJECTED, scope or REQUEST
    else:
        kind = UNKNOWN

    if kind in (RATE, CREDIT) and not scope:
        scope = ACCOUNT
    if kind == QUOTA and not scope:
        # An allowance that does not say when it rolls. Not the ten minutes an
        # account-wide pause gets: Google's per-minute ceiling arrives with a
        # structured violation naming it, so a quota that reached here without a
        # window is a wider one than that. Fifteen minutes is long enough not to
        # hammer it and short enough to have the service back within the hour.
        scope = HOUR
    return Fault(
        status=status,
        service=service,
        kind=kind,
        scope=scope,
        retry_after=min(asked, MAX_RETRY_AFTER) if asked > 0 else 0.0,
        said=text[:200],
        model=model,
    )
