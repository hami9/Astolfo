"""Settings and keys that can be changed from Telegram instead of the .env file.

Values are layered: the environment supplies the defaults, the database supplies
whatever the owner has since changed, and the database wins. Nothing here has its
own list of setting names — the fields of `Settings` and their environment names
are the single source of truth, so a new setting is editable the moment it exists.
"""

from __future__ import annotations

import logging
import os
from dataclasses import fields
from typing import Any

from .config import ConfigError, Settings, _coerce
from .crypto import SecretBox, SecretsUnavailable
from .db import Database, open_database

log = logging.getLogger(__name__)

# Changing these from a chat would cut the bot off from Telegram, move the
# database out from under itself, or hand the panel to somebody else.
LOCKED = frozenset({"telegram_token", "data_dir", "master_id", "master_username"})

# Keys live in the secrets table, encrypted, never in the settings table.
SECRET_ENV = (
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "GITHUB_MODELS_TOKEN",
    "CEREBRAS_API_KEY",
)


def editable() -> dict[str, str]:
    """Setting name to environment variable, for everything the panel may change."""
    return {
        f.name: f.metadata.get("env", "")
        for f in fields(Settings)
        if f.name not in LOCKED and f.metadata.get("env")
    }


def annotation(name: str) -> str:
    for f in fields(Settings):
        if f.name == name:
            return str(f.type)
    raise KeyError(name)


def parse(name: str, raw: str) -> Any:
    """The value a setting would take from this text, or a ConfigError."""
    try:
        return _coerce(annotation(name), raw)
    except KeyError:
        raise ConfigError(f"there is no setting called {name}") from None
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} does not accept {raw!r} ({exc})") from exc


def apply(settings: Settings, overrides: dict[str, str]) -> Settings:
    """Layer stored overrides on top of what the environment gave us."""
    allowed = editable()
    values: dict[str, Any] = {}
    for key, raw in overrides.items():
        if key not in allowed:
            log.warning("ignoring stored setting %r: not a setting the bot has", key)
            continue
        try:
            values[key] = parse(key, raw)
        except ConfigError as exc:
            log.warning("ignoring stored setting: %s", exc)
    return settings.replace(**values) if values else settings


def export_secrets(db: Database, box: SecretBox) -> list[str]:
    """Put stored keys into the environment, where every service already looks.

    python-dotenv does the same thing with the .env file; doing it here means the
    provider code keeps reading `os.environ` and does not need to know that some
    keys now arrive from the database.
    """
    exported = []
    for name in SECRET_ENV:
        blob = db.secret(name)
        if blob is None:
            continue
        value = box.decrypt(blob)
        if value:
            os.environ[name] = value
            exported.append(name)
    if exported:
        log.info("loaded %d stored key(s): %s", len(exported), ", ".join(exported))
    return exported


def store_secret(db: Database, box: SecretBox, name: str, value: str, *, by: int) -> None:
    """Save a key and make it live for the next call, without a restart."""
    if not box.available:
        raise SecretsUnavailable("encryption is not available on this install")
    db.set_secret(name, box.encrypt(value), by=by)
    os.environ[name] = value
    db.record(actor=by, action="set_secret", detail=name)


def forget_secret(db: Database, name: str, *, by: int) -> None:
    db.delete_secret(name)
    os.environ.pop(name, None)
    db.record(actor=by, action="clear_secret", detail=name)


def set_override(db: Database, key: str, raw: str, *, by: int) -> Any:
    """Validate, store, and return the value the setting will now hold."""
    if key in LOCKED:
        raise ConfigError(f"{key} can only be changed on the server")
    if key not in editable():
        raise ConfigError(f"there is no setting called {key}")
    value = parse(key, raw)
    db.set_override(key, raw, by=by)
    db.record(actor=by, action="set_setting", detail=f"{key}={raw}"[:200])
    return value


def clear_override(db: Database, key: str, *, by: int) -> None:
    db.clear_override(key)
    db.record(actor=by, action="clear_setting", detail=key)


def bootstrap() -> tuple[Settings, Database, SecretBox]:
    """Assemble the settings the bot actually runs with.

    The order matters: the database has to be opened before the settings are
    checked, because the key the check looks for may only exist in the database.
    """
    probe = Settings.from_env(validate=False)
    db = open_database(probe.data_dir)
    box = SecretBox(probe.data_dir)
    export_secrets(db, box)

    settings = apply(Settings.from_env(validate=False), db.overrides())
    settings.validate()
    return settings, db, box
