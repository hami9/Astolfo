"""Settings and keys the owner can change without touching the server."""

from __future__ import annotations

import os
import stat

import pytest

from astolfo import settings_store
from astolfo.config import ConfigError, Settings
from astolfo.crypto import SecretBox, mask
from astolfo.db import open_database


def _box(settings) -> SecretBox:
    return SecretBox(settings.data_dir)


# -- layering -------------------------------------------------------------
def test_a_stored_setting_wins_over_the_environment(settings):
    db = open_database(settings.data_dir)
    settings_store.set_override(db, "free_mode", "1", by=1)

    assert settings.free_mode is False
    assert settings_store.apply(settings, db.overrides()).free_mode is True


def test_values_are_coerced_to_the_type_of_the_field(settings):
    db = open_database(settings.data_dir)
    settings_store.set_override(db, "group_reply_chance", "0.42", by=1)
    settings_store.set_override(db, "providers", "openrouter, google", by=1)

    live = settings_store.apply(settings, db.overrides())
    assert live.group_reply_chance == 0.42
    assert live.providers == ["openrouter", "google"]


def test_a_value_the_field_cannot_hold_is_refused(settings):
    db = open_database(settings.data_dir)
    with pytest.raises(ConfigError):
        settings_store.set_override(db, "max_history", "lots", by=1)
    assert db.overrides() == {}


def test_an_unknown_setting_is_refused(settings):
    db = open_database(settings.data_dir)
    with pytest.raises(ConfigError):
        settings_store.set_override(db, "make_coffee", "1", by=1)


def test_the_bot_token_cannot_be_changed_from_a_chat(settings):
    db = open_database(settings.data_dir)
    with pytest.raises(ConfigError):
        settings_store.set_override(db, "telegram_token", "stolen", by=1)


def test_a_setting_that_no_longer_exists_is_skipped_not_fatal(settings):
    """An old row must never stop the bot from starting."""
    db = open_database(settings.data_dir)
    db.set_override("setting_from_a_past_release", "1")
    assert settings_store.apply(settings, db.overrides()) == settings


def test_clearing_an_override_returns_to_the_environment(settings):
    db = open_database(settings.data_dir)
    settings_store.set_override(db, "free_mode", "1", by=1)
    settings_store.clear_override(db, "free_mode", by=1)
    assert settings_store.apply(settings, db.overrides()).free_mode is False


def test_every_editable_setting_survives_a_round_trip(settings):
    """Text in, value out: the panel must not be able to corrupt a setting."""
    for name in settings_store.editable():
        current = getattr(settings, name)
        if isinstance(current, list):
            raw = ",".join(str(item) for item in current)
        else:
            # An optional setting spells "unset" as an empty value, not as "None".
            raw = "" if current is None else str(current)
        assert settings_store.parse(name, raw) == current, name


# -- keys -----------------------------------------------------------------
def test_a_key_survives_encryption(settings):
    box = _box(settings)
    blob = box.encrypt("sk-or-v1-secret")
    assert b"sk-or" not in blob
    assert box.decrypt(blob) == "sk-or-v1-secret"


def test_the_key_file_is_private(settings):
    box = _box(settings)
    assert stat.S_IMODE(os.stat(box.path).st_mode) & 0o077 == 0


def test_a_stored_key_reaches_the_environment(settings, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    db, box = open_database(settings.data_dir), _box(settings)

    settings_store.store_secret(db, box, "GOOGLE_API_KEY", "g-key", by=1)
    assert os.environ["GOOGLE_API_KEY"] == "g-key"

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert settings_store.export_secrets(db, box) == ["GOOGLE_API_KEY"]
    assert os.environ["GOOGLE_API_KEY"] == "g-key"

    settings_store.forget_secret(db, "GOOGLE_API_KEY", by=1)
    assert "GOOGLE_API_KEY" not in os.environ


def test_a_key_written_with_another_encryption_key_is_reported_not_used(settings, tmp_path):
    db = open_database(settings.data_dir)
    stranger = SecretBox(str(tmp_path / "elsewhere"))
    db.set_secret("GOOGLE_API_KEY", stranger.encrypt("g-key"))

    assert settings_store.export_secrets(db, _box(settings)) == []


def test_setting_a_key_is_written_to_the_audit_trail(settings):
    db, box = open_database(settings.data_dir), _box(settings)
    settings_store.store_secret(db, box, "GROQ_API_KEY", "gq-key", by=77)

    entry = db.audit_trail()[0]
    assert entry["action"] == "set_secret"
    assert entry["actor"] == 77
    assert "gq-key" not in entry["detail"], "the key itself must never be written down"


def test_masking_shows_enough_to_recognise_and_not_enough_to_use():
    assert mask("sk-or-v1-15e5d7781a9767ff") == "sk-or-…67ff"
    assert mask("") == "(not set)"
    assert "15e5d778" not in mask("sk-or-v1-15e5d7781a9767ff")


# -- bootstrap ------------------------------------------------------------
def test_the_bot_starts_with_a_key_that_only_exists_in_the_database(settings, monkeypatch):
    """The whole point: no .env edit needed to give the bot a key."""
    monkeypatch.setenv("DATA_DIR", settings.data_dir)
    db, box = open_database(settings.data_dir), _box(settings)
    settings_store.store_secret(db, box, "OPENROUTER_API_KEY", "sk-or-from-db", by=1)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    live = settings_store.bootstrap()[0]
    assert live.api_key == "sk-or-from-db"


def test_a_missing_key_is_still_a_startup_error(settings, monkeypatch):
    monkeypatch.setenv("DATA_DIR", settings.data_dir)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        settings_store.bootstrap()


def test_settings_from_env_can_defer_its_checks(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert Settings.from_env(validate=False).api_key == ""
