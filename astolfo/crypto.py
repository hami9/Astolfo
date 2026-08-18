"""Encryption for the keys the bot stores.

The database is one file that gets copied around — backups, a scp to a laptop, a
stray `tar` — and an API key sitting in it in clear text travels with every copy.
Fernet with a key file next to it fixes that much. It is not protection against
someone who already has the server: they can read the key file too. What it buys
is that a leaked database alone is not a leaked key.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

KEY_FILE = "secret.key"

try:
    from cryptography.fernet import Fernet, InvalidToken

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the requirements install
    Fernet = None
    InvalidToken = Exception
    AVAILABLE = False


class SecretsUnavailable(RuntimeError):
    """Raised when a key cannot be stored because encryption is not installed."""


class SecretBox:
    """Encrypts and decrypts with a key file the bot owns."""

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, KEY_FILE)
        self._fernet = self._load() if AVAILABLE else None
        if not AVAILABLE:
            log.warning(
                "the cryptography package is missing, so stored keys are unavailable; "
                "install it with: pip install -r requirements.txt"
            )

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def _load(self):
        try:
            with open(self.path, "rb") as fh:
                key = fh.read().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # Written 0600 from the start: creating it readable and fixing it after
            # leaves a window where anyone on the box can take the key.
            handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "wb") as fh:
                fh.write(key)
            log.info("generated a new encryption key at %s", self.path)
        except OSError as exc:
            log.error("could not read the encryption key: %s", exc)
            return None
        try:
            return Fernet(key)
        except Exception as exc:
            log.error("the encryption key at %s is not usable: %s", self.path, exc)
            return None

    def encrypt(self, value: str) -> bytes:
        if self._fernet is None:
            raise SecretsUnavailable("encryption is not available on this install")
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str | None:
        """The plain value, or None when it cannot be read with this key."""
        if self._fernet is None:
            return None
        try:
            return self._fernet.decrypt(blob).decode("utf-8")
        except InvalidToken:
            log.error("a stored secret does not match the current encryption key")
            return None


def mask(value: str) -> str:
    """A key the owner can recognise but nobody can use, safe to show in chat."""
    if not value:
        return "(not set)"
    if len(value) <= 12:
        return f"{value[:2]}…{value[-2:]}"
    return f"{value[:6]}…{value[-4:]}"
