import httpx
import pytest

from astolfo.llm import LLMClient, cacheable_system, parse_json


def _completion(text="hello", cost=0.001, cached=10):
    return {
        "model": "test/model",
        "choices": [
            {
                "message": {
                    "content": text,
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {"title": "Source", "url": "https://example.com"},
                        }
                    ],
                }
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cost": cost,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


def _client(settings, handler) -> LLMClient:
    return LLMClient(settings, transport=httpx.MockTransport(handler))


async def test_successful_call_parses_usage_and_citations(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = __import__("json").loads(request.content)
        return httpx.Response(200, json=_completion())

    client = _client(settings, handler)
    result = await client.chat([{"role": "user", "content": "hi"}], model="test/model")

    assert result.ok and result.text == "hello"
    assert result.usage.cost == 0.001
    assert result.usage.cached_tokens == 10
    assert result.citations[0].url == "https://example.com"
    assert seen["payload"]["usage"] == {"include": True}
    await client.aclose()


async def test_web_plugin_is_attached_only_when_requested(settings):
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(__import__("json").loads(request.content))
        return httpx.Response(200, json=_completion())

    client = _client(settings, handler)
    await client.chat([{"role": "user", "content": "hi"}], model="m", web=False)
    await client.chat([{"role": "user", "content": "hi"}], model="m", web=True)

    assert "plugins" not in payloads[0]
    assert payloads[1]["plugins"][0]["id"] == "web"
    await client.aclose()


async def test_retries_on_server_error(settings, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json=_completion("recovered"))

    client = _client(settings.replace(max_retries=4), handler)
    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert result.text == "recovered"
    assert calls["n"] == 3
    await client.aclose()


async def test_unsupported_reasoning_param_is_dropped(settings, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        payloads.append(payload)
        if "reasoning" in payload:
            return httpx.Response(400, text="unsupported parameter: reasoning")
        return httpx.Response(200, json=_completion())

    client = _client(settings, handler)
    result = await client.chat(
        [{"role": "user", "content": "hi"}], model="m", reasoning={"effort": "high"}
    )

    assert result.ok
    assert len(payloads) == 2
    assert "reasoning" not in payloads[1]
    await client.aclose()


async def test_auth_error_is_not_retried(settings):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="no key")

    client = _client(settings, handler)
    result = await client.chat([{"role": "user", "content": "hi"}], model="m")

    assert not result.ok
    assert "invalid API key" in result.error
    assert calls["n"] == 1
    await client.aclose()


async def test_catalog_drives_model_fallback(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "openai/gpt-4o-mini"}]})

    client = _client(settings, handler)
    await client.load_catalog()

    assert client.resolve("missing/model") == "openai/gpt-4o-mini"
    assert client.resolve("openai/gpt-4o-mini") == "openai/gpt-4o-mini"
    await client.aclose()


def test_cacheable_system_marks_anthropic_only():
    plain = cacheable_system("text", "google/gemini-2.5-flash", True)
    assert plain["content"] == "text"

    marked = cacheable_system("text", "anthropic/claude-3.5-haiku", True)
    assert marked["content"][0]["cache_control"] == {"type": "ephemeral"}

    disabled = cacheable_system("text", "anthropic/claude-3.5-haiku", False)
    assert disabled["content"] == "text"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"mode":"fast"}', {"mode": "fast"}),
        ('```json\n{"mode":"think"}\n```', {"mode": "think"}),
        ('sure thing: {"web": true} hope that helps', {"web": True}),
        ("no json here", None),
        ("[1, 2, 3]", None),
    ],
)
def test_parse_json(raw, expected):
    assert parse_json(raw) == expected


async def _no_sleep(_seconds):
    return None
