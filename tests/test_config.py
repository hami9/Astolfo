import pytest

from astolfo.config import ConfigError, Settings


def test_defaults_load(settings):
    assert settings.model_fast
    assert settings.max_history > 0
    assert settings.chat_url.endswith("/chat/completions")
    assert settings.models_url.endswith("/models")


def test_env_coercion(monkeypatch):
    monkeypatch.setenv("GROUP_REPLY_CHANCE", "0.75")
    monkeypatch.setenv("WEB_SEARCH", "false")
    monkeypatch.setenv("MAX_HISTORY", "10")
    monkeypatch.setenv("FALLBACK_MODELS", "a/b, c/d")
    monkeypatch.setenv("ADMIN_IDS", "12, not-a-number ,34")

    loaded = Settings.from_env()
    assert loaded.group_reply_chance == 0.75
    assert loaded.web_search is False
    assert loaded.max_history == 10
    assert loaded.fallback_models == ["a/b", "c/d"]
    assert loaded.admin_ids == [12, 34]


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env()
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_invalid_number(monkeypatch):
    monkeypatch.setenv("MAX_HISTORY", "many")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_replace_is_non_mutating(settings):
    other = settings.replace(max_history=99)
    assert other.max_history == 99
    assert settings.max_history != 99
