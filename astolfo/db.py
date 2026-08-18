"""The bot's own database: chats, people, settings, secrets and an audit trail.

SQLite because it is one file, needs no server, and survives a restart on a 1 GB
box. The schema is versioned so later releases add to it instead of rewriting it.

What is stored is deliberately narrow: who is in which chat and how active they
are, never a single line of what anyone said.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    type        TEXT    NOT NULL DEFAULT '',
    title       TEXT    NOT NULL DEFAULT '',
    username    TEXT    NOT NULL DEFAULT '',
    joined_at   REAL,
    left_at     REAL,
    last_seen   REAL,
    muted       INTEGER NOT NULL DEFAULT 0,
    reply_chance REAL,
    forced_mode TEXT,
    locale      TEXT,
    notes       TEXT    NOT NULL DEFAULT '',
    messages    INTEGER NOT NULL DEFAULT 0,
    replies     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL DEFAULT '',
    username    TEXT    NOT NULL DEFAULT '',
    first_seen  REAL,
    last_seen   REAL,
    messages    INTEGER NOT NULL DEFAULT 0,
    blocked     INTEGER NOT NULL DEFAULT 0,
    is_master   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS members (
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    first_seen  REAL,
    last_seen   REAL,
    messages    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, chat_id)
);
CREATE INDEX IF NOT EXISTS members_by_chat ON members (chat_id, last_seen DESC);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL,
    updated_by  INTEGER
);

CREATE TABLE IF NOT EXISTS secrets (
    name        TEXT PRIMARY KEY,
    value       BLOB NOT NULL,
    updated_at  REAL,
    updated_by  INTEGER
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL,
    actor       INTEGER,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_by_time ON audit (at DESC);
"""

TABLES = ("chats", "users", "members", "settings", "secrets", "audit")


class Database:
    """A thin, synchronous wrapper. Writes are small and land in microseconds."""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
            _restrict(directory, 0o700)

        # check_same_thread=False: python-telegram-bot runs handlers on the event
        # loop but background tasks may land elsewhere. The lock below, not the
        # thread identity, is what keeps writes ordered.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        _restrict(path, 0o600)

        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        current = self._db.execute("PRAGMA user_version").fetchone()[0]
        if current == SCHEMA_VERSION:
            return
        if current > SCHEMA_VERSION:
            log.warning(
                "database was written by a newer version (%s > %s); leaving it alone",
                current,
                SCHEMA_VERSION,
            )
            return
        # Future versions add their steps here; the table definitions above are
        # written with IF NOT EXISTS so a fresh file and an upgrade agree.
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()
        if current:
            log.info("database schema upgraded from %s to %s", current, SCHEMA_VERSION)

    # -- plumbing ---------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            return cursor

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def counts(self) -> dict[str, int]:
        return {t: self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in TABLES}

    def vacuum(self) -> None:
        with self._lock:
            self._db.execute("VACUUM")

    # -- chats ------------------------------------------------------------
    def seen_chat(
        self, chat_id: int, *, kind: str = "", title: str = "", username: str = ""
    ) -> None:
        now = time.time()
        self.execute(
            """
            INSERT INTO chats (chat_id, type, title, username, joined_at, last_seen, messages)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                type      = CASE WHEN excluded.type <> '' THEN excluded.type ELSE chats.type END,
                title     = CASE WHEN excluded.title <> '' THEN excluded.title ELSE chats.title END,
                username  = CASE WHEN excluded.username <> ''
                                 THEN excluded.username ELSE chats.username END,
                last_seen = excluded.last_seen,
                left_at   = NULL,
                messages  = chats.messages + 1
            """,
            (chat_id, kind, title, username, now, now),
        )

    def joined_chat(self, chat_id: int, *, kind: str, title: str, username: str = "") -> None:
        now = time.time()
        self.execute(
            """
            INSERT INTO chats (chat_id, type, title, username, joined_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                type = excluded.type, title = excluded.title, username = excluded.username,
                joined_at = COALESCE(chats.joined_at, excluded.joined_at),
                left_at = NULL, last_seen = excluded.last_seen
            """,
            (chat_id, kind, title, username, now, now),
        )

    def left_chat(self, chat_id: int) -> None:
        self.execute("UPDATE chats SET left_at = ? WHERE chat_id = ?", (time.time(), chat_id))

    def count_reply(self, chat_id: int) -> None:
        self.execute("UPDATE chats SET replies = replies + 1 WHERE chat_id = ?", (chat_id,))

    def save_chat_state(self, chat_id: int, **columns: Any) -> None:
        """Persist the per-chat knobs the bot itself changes."""
        allowed = {"muted", "reply_chance", "forced_mode", "locale", "notes", "title"}
        fields = {k: v for k, v in columns.items() if k in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        self.execute(
            f"UPDATE chats SET {assignments} WHERE chat_id = ?",
            (*fields.values(), chat_id),
        )

    def chat_settings(self) -> list[sqlite3.Row]:
        """Rows worth restoring into memory after a restart."""
        return self.query(
            """
            SELECT chat_id, notes, reply_chance, forced_mode, muted, title, locale
            FROM chats
            WHERE notes <> '' OR reply_chance IS NOT NULL OR forced_mode IS NOT NULL OR muted = 1
            """
        )

    def list_chats(self, *, active_only: bool = True, limit: int = 50) -> list[sqlite3.Row]:
        where = "WHERE left_at IS NULL" if active_only else ""
        return self.query(
            f"""
            SELECT c.*, (SELECT COUNT(*) FROM members m WHERE m.chat_id = c.chat_id) AS people
            FROM chats c {where}
            ORDER BY c.last_seen IS NULL, c.last_seen DESC
            LIMIT ?
            """,
            (limit,),
        )

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))

    # -- people -----------------------------------------------------------
    def seen_member(self, *, user_id: int, chat_id: int, name: str, username: str = "") -> None:
        now = time.time()
        self.execute(
            """
            INSERT INTO users (user_id, name, username, first_seen, last_seen, messages)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                username = CASE WHEN excluded.username <> ''
                                THEN excluded.username ELSE users.username END,
                last_seen = excluded.last_seen,
                messages = users.messages + 1
            """,
            (user_id, name, username, now, now),
        )
        self.execute(
            """
            INSERT INTO members (user_id, chat_id, name, first_seen, last_seen, messages)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                name = excluded.name, last_seen = excluded.last_seen,
                messages = members.messages + 1
            """,
            (user_id, chat_id, name, now, now),
        )

    def members(self, chat_id: int, *, limit: int = 30) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT m.*, u.username, u.blocked
            FROM members m LEFT JOIN users u ON u.user_id = m.user_id
            WHERE m.chat_id = ?
            ORDER BY m.messages DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )

    def find_people(self, needle: str, *, limit: int = 20) -> list[sqlite3.Row]:
        if needle.lstrip("-").isdigit():
            return self.query("SELECT * FROM users WHERE user_id = ?", (int(needle),))
        pattern = f"%{needle}%"
        return self.query(
            """
            SELECT * FROM users WHERE name LIKE ? OR username LIKE ?
            ORDER BY last_seen DESC LIMIT ?
            """,
            (pattern, pattern, limit),
        )

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        self.execute(
            "UPDATE users SET blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id)
        )

    def blocked_ids(self) -> set[int]:
        return {row["user_id"] for row in self.query("SELECT user_id FROM users WHERE blocked = 1")}

    # -- master -----------------------------------------------------------
    def master_id(self) -> int | None:
        row = self.one("SELECT user_id FROM users WHERE is_master = 1 LIMIT 1")
        return int(row["user_id"]) if row else None

    def claim_master(self, user_id: int, *, name: str = "", username: str = "") -> None:
        now = time.time()
        self.execute(
            """
            INSERT INTO users (user_id, name, username, first_seen, last_seen, is_master)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET is_master = 1
            """,
            (user_id, name, username, now, now),
        )
        self.execute("UPDATE users SET is_master = 0 WHERE user_id <> ?", (user_id,))

    # -- settings ---------------------------------------------------------
    def overrides(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.query("SELECT key, value FROM settings")}

    def set_override(self, key: str, value: str, *, by: int | None = None) -> None:
        self.execute(
            """
            INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (key, value, time.time(), by),
        )

    def clear_override(self, key: str) -> None:
        self.execute("DELETE FROM settings WHERE key = ?", (key,))

    # -- secrets ----------------------------------------------------------
    def secret(self, name: str) -> bytes | None:
        row = self.one("SELECT value FROM secrets WHERE name = ?", (name,))
        return bytes(row["value"]) if row else None

    def set_secret(self, name: str, value: bytes, *, by: int | None = None) -> None:
        self.execute(
            """
            INSERT INTO secrets (name, value, updated_at, updated_by) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (name, value, time.time(), by),
        )

    def delete_secret(self, name: str) -> None:
        self.execute("DELETE FROM secrets WHERE name = ?", (name,))

    def secret_names(self) -> list[sqlite3.Row]:
        return self.query("SELECT name, updated_at FROM secrets ORDER BY name")

    # -- audit ------------------------------------------------------------
    def record(self, *, actor: int | None, action: str, detail: str = "") -> None:
        self.execute(
            "INSERT INTO audit (at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (time.time(), actor, action, detail[:500]),
        )

    def audit_trail(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM audit ORDER BY at DESC LIMIT ?", (limit,))


def _restrict(path: str, mode: int) -> None:
    """Keep the database and its directory readable only by the bot's own user."""
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.debug("could not set permissions on %s: %s", path, exc)


def open_database(data_dir: str) -> Database:
    return Database(os.path.join(data_dir, "astolfo.db"))
