"""Reading what each service actually said, rather than flattening it.

The bug this exists for, in the owner's words: the models report a monthly limit
and then run again a few minutes later. Both halves of that came from the same
place - `HTTP 429: cohere limit`, with the body thrown away. A trial key's
twenty-calls-a-minute and a spent monthly allowance arrived as the same sentence
and got the same sixty-second rest, which is right for one and useless for the
other.

Every body below is the shape the named service really returns.
"""

from __future__ import annotations

from astolfo import faults
from astolfo.faults import (
    AUTH,
    BLOCKED,
    CREDIT,
    DAY,
    MINUTE,
    MONTH,
    QUOTA,
    RATE,
    REJECTED,
    SERVER,
    read,
)


# -- the two that were being confused --------------------------------------
def test_a_trial_keys_minute_is_not_a_month() -> None:
    """Cohere says the window out loud, and it is sixty seconds wide."""
    fault = read(
        429,
        '{"message":"You are using a Trial key, which is limited to 20 API calls / minute."}',
        service="cohere",
    )

    assert (fault.kind, fault.scope) == (RATE, MINUTE)
    assert fault.wait <= 120, "back in a minute, which is when it really is back"


def test_a_spent_monthly_allowance_is_not_a_minute() -> None:
    """HuggingFace's free tier is monthly credits, and it says so."""
    fault = read(
        402,
        '{"error":"You have exceeded your monthly included credits for Inference Providers."}',
        service="huggingface",
    )

    assert (fault.kind, fault.scope) == (QUOTA, MONTH)
    assert fault.wait > 3600, "not worth retrying in a minute"


def test_the_two_do_not_get_the_same_rest() -> None:
    """The whole point. Same class of status, three orders of magnitude apart."""
    minute = read(429, '{"message":"limited to 20 API calls / minute"}', service="cohere")
    month = read(402, '{"error":"exceeded your monthly included credits"}', service="hf")

    assert month.wait > minute.wait * 50


# -- each service's own dialect --------------------------------------------
def test_google_names_the_exact_quota_it_refused() -> None:
    """Nobody else does. The id is the difference between a minute and tomorrow."""
    per_minute = read(
        429,
        '{"error":{"code":429,"message":"Resource has been exhausted","details":['
        '{"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":'
        '[{"quotaId":"GenerateRequestsPerMinutePerProjectPerModel"}]},'
        '{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"31s"}]}}',
        service="google",
    )
    per_day = read(
        429,
        '{"error":{"code":429,"message":"Quota exceeded","details":['
        '{"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":'
        '[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel"}]}]}}',
        service="google",
    )

    assert per_minute.scope == MINUTE
    assert per_minute.retry_after == 31.0, "read from RetryInfo, not from the text"
    assert per_day.scope == DAY
    assert per_day.wait > per_minute.wait * 10


def test_google_calling_a_minute_a_quota_is_still_a_minute() -> None:
    """It says "resource exhausted" for a ceiling that rolls in sixty seconds.
    A window that short is a rate limit whatever words are wrapped around it."""
    fault = read(
        429,
        '{"error":{"details":[{"@type":"x/QuotaFailure","violations":'
        '[{"quotaId":"RequestsPerMinute"}]}]}}',
        service="google",
    )

    assert fault.kind == RATE


def test_groq_says_when_in_its_message() -> None:
    fault = read(
        429,
        '{"error":{"message":"Rate limit reached for model llama-3.3-70b on tokens per day '
        '(TPD): Limit 100000, Used 100000. Please try again in 6m30s.","code":'
        '"rate_limit_exceeded"}}',
        service="groq",
    )

    assert fault.scope == DAY
    assert fault.retry_after == 390.0, "6m30s, as it asked"


def test_openrouters_free_daily_ceiling_is_read_as_daily() -> None:
    fault = read(
        429, '{"error":{"message":"Rate limit exceeded: free-models-per-day","code":429}}',
        service="openrouter",
    )

    assert (fault.kind, fault.scope) == (QUOTA, DAY)


def test_an_empty_wallet_is_never_a_short_rest() -> None:
    """No amount of waiting fills it, so every retry is a wasted call."""
    for body in (
        '{"error":{"message":"Insufficient credits. Add more at .../credits","code":402}}',
        '{"detail":{"error":"Payment required. Please add credits."}}',
    ):
        fault = read(402, body, service="x")
        assert fault.kind == CREDIT, body
        assert fault.terminal
        assert fault.wait > 3600


def test_a_wrong_key_and_a_blocked_request_are_told_apart() -> None:
    """Both are 403-shaped problems and only one is fixed by a new key. Cerebras
    returns a Cloudflare 403 for some datacentre ranges while a genuinely wrong
    key gets a clean 401 with a message."""
    wrong = read(401, '{"message":"Wrong API Key","code":"wrong_api_key"}', service="cerebras")
    edge = read(403, "", service="cerebras")

    assert wrong.kind == AUTH
    assert edge.kind == BLOCKED
    assert "never reached" in edge.summary


def test_a_403_that_does_name_the_key_is_an_auth_problem() -> None:
    assert read(403, '{"error":"Invalid API key"}', service="x").kind == AUTH


def test_their_side_breaking_is_not_our_problem_to_wait_out() -> None:
    fault = read(500, '{"error":"internal server error"}', service="sambanova")
    assert fault.kind == SERVER
    assert fault.wait == 0.0, "retried in place with backoff, not rested"


def test_a_rejected_request_is_not_a_limit() -> None:
    fault = read(400, '{"error":{"message":"model does not support image input"}}', service="x")
    assert fault.kind == REJECTED
    assert "image" in fault.said


# -- never worse than what it replaced -------------------------------------
def test_an_unreadable_body_still_produces_a_fault() -> None:
    for body in ("", "<html>502 Bad Gateway</html>", "{not json", "null"):
        fault = read(429, body, service="x")
        assert fault.status == 429
        assert fault.summary.startswith("x: HTTP 429"), body


def test_the_quoted_part_is_only_ever_what_they_said() -> None:
    """Nothing in the summary between quotes is written by this module."""
    fault = read(429, '{"message":"slow down please"}', service="x")

    assert '"slow down please"' in fault.summary
    assert fault.said == "slow down please"


def test_a_retry_after_header_wins_over_a_guess() -> None:
    fault = read(429, "{}", service="x", retry_after=12.0)
    assert fault.wait == 12.0


def test_a_service_cannot_ask_for_an_unreasonable_wait() -> None:
    """A retry-after of a week is a fact worth recording, not one worth obeying."""
    fault = read(429, "{}", service="x", retry_after=7 * 86400.0)
    assert fault.wait == faults.MAX_RETRY_AFTER


def test_every_kind_has_words_a_person_can_act_on() -> None:
    """The panel prints this. "auth" and "blocked" are the same status code and
    completely different problems."""
    for kind in (RATE, QUOTA, CREDIT, AUTH, BLOCKED, REJECTED, SERVER, faults.UNKNOWN):
        phrase = faults.SAYS[kind]
        assert phrase != kind, kind
        assert len(phrase.split()) >= 3, f"{kind}: {phrase!r} is still jargon"


def test_the_summary_says_where_what_and_how_long() -> None:
    fault = read(
        429, '{"message":"limited to 20 API calls / minute"}', service="cohere", model="command-r"
    )
    line = fault.summary

    assert "cohere/command-r" in line
    assert "HTTP 429" in line
    assert "too many requests per minute" in line


# -- and the client acts on what it read -----------------------------------
async def test_a_daily_ceiling_rests_the_service_for_hours_not_a_minute(settings, monkeypatch):
    """The bug end to end: outside free mode a 429 used to be retried in place
    with a twenty-second backoff, whatever window it named."""
    import httpx

    from astolfo.llm import LLMClient

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            429, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(
        settings.replace(providers=["openrouter"]), transport=httpx.MockTransport(refuse)
    )
    result = await client.chat([{"role": "user", "content": "hi"}], model="m")
    await client.aclose()

    assert not result.ok
    assert "per day" in result.error
    assert client.providers[0].paused_until > 0, "rested, not retried in place"


async def test_what_a_service_said_is_kept_for_the_panel(settings, monkeypatch):
    import httpx

    from astolfo.llm import LLMClient

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(402, json={"error": {"message": "Insufficient credits."}})

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = LLMClient(
        settings.replace(providers=["openrouter"]), transport=httpx.MockTransport(refuse)
    )
    await client.chat([{"role": "user", "content": "hi"}], model="m")
    kept = client.recent_faults("openrouter")
    await client.aclose()

    assert kept, "the panel has something to print"
    assert kept[0][1].kind == CREDIT
    assert "Insufficient credits." in kept[0][1].summary
