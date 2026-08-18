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

SCHEMA_VERSION = 2

# Provider keys used to live in `secrets`, one per service. They are credentials
# now, so a service can hold more than one and each carries its own state.
KEY_ENV_TO_SERVICE = {
    "OPENROUTER_API_KEY": "openrouter",
    "GOOGLE_API_KEY": "google",
    "GROQ_API_KEY": "groq",
    "GITHUB_MODELS_TOKEN": "github",
    "CEREBRAS_API_KEY": "cerebras",
}

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

-- A row here either overrides the preset of the same name or defines a service
-- the code has never heard of. An empty column means "keep what the preset says".
CREATE TABLE IF NOT EXISTS services (
    name          TEXT PRIMARY KEY,
    base_url      TEXT    NOT NULL DEFAULT '',
    models        TEXT    NOT NULL DEFAULT '',
    vision_models TEXT    NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    position      INTEGER NOT NULL DEFAULT 100,
    custom        INTEGER NOT NULL DEFAULT 0,
    discovers_free_models INTEGER NOT NULL DEFAULT 0,
    openrouter_extensions INTEGER NOT NULL DEFAULT 0,
    rested_until  REAL    NOT NULL DEFAULT 0,
    last_error    TEXT    NOT NULL DEFAULT '',
    added_at      REAL
);

-- Wall clock, not monotonic: a quota that runs out until tomorrow has to still
-- be known tomorrow, on the other side of a restart.
CREATE TABLE IF NOT EXISTS credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    service      TEXT    NOT NULL,
    label        TEXT    NOT NULL DEFAULT '',
    value        BLOB    NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    added_at     REAL,
    last_used    REAL,
    last_ok      REAL,
    last_error   TEXT    NOT NULL DEFAULT '',
    rested_until REAL    NOT NULL DEFAULT 0,
    requests     INTEGER NOT NULL DEFAULT 0,
    failures     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS credentials_by_service ON credentials (service, position);

CREATE TABLE IF NOT EXISTS service_usage (
    day       TEXT    NOT NULL,
    service   TEXT    NOT NULL,
    requests  INTEGER NOT NULL DEFAULT 0,
    failures  INTEGER NOT NULL DEFAULT 0,
    tokens    INTEGER NOT NULL DEFAULT 0,
    cost      REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (day, service)
);
"""

TABLES = (
    "chats",
    "users",
    "members",
    "settings",
    "secrets",
    "audit",
    "services",
    "credentials",
    "service_usage",
)


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
        # The table definitions above run with IF NOT EXISTS, so a fresh file and
        # an upgrade end up in the same place; only data moves belong here.
        if current and current < 2:
            self._adopt_stored_keys()

        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()
        if current:
            log.info("database schema upgraded from %s to %s", current, SCHEMA_VERSION)

    def _adopt_stored_keys(self) -> None:
        """Carry keys saved under the old one-per-service scheme into credentials."""
        moved = 0
        for row in self._db.execute("SELECT name, value FROM secrets").fetchall():
            service = KEY_ENV_TO_SERVICE.get(row["name"])
            if not service:
                continue
            self._db.execute(
                """
                INSERT INTO credentials (service, label, value, position, added_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (service, "moved from secrets", row["value"], time.time()),
            )
            # Removed from the old table, not copied: two rows holding the same
            # key would show up as two keys for one service.
            self._db.execute("DELETE FROM secrets WHERE name = ?", (row["name"],))
            moved += 1
        if moved:
            log.info("moved %d stored key(s) into the credentials table", moved)

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
        # Table names come from TABLES above, never from a caller.
        return {
            t: self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]  # noqa: S608
            for t in TABLES
        }

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
            # noqa: S608 - assignments is built from the allowlist above; the
            # values themselves are still bound, never formatted in
            f"UPDATE chats SET {assignments} WHERE chat_id = ?",  # noqa: S608
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
        # The filter is a bound value rather than a piece of assembled SQL, so
        # this query is a constant no matter who calls it.
        return self.query(
            """
            SELECT c.*, (SELECT COUNT(*) FROM members m WHERE m.chat_id = c.chat_id) AS people
            FROM chats c
            WHERE ? = 0 OR c.left_at IS NULL
            ORDER BY c.last_seen IS NULL, c.last_seen DESC
            LIMIT ?
            """,
            (1 if active_only else 0, limit),
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
        # An upsert, not an update: blocking somebody the bot has never recorded
        # would otherwise change nothing and be forgotten on the next start.
        now = time.time()
        self.execute(
            """
            INSERT INTO users (user_id, first_seen, last_seen, blocked) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET blocked = excluded.blocked
            """,
            (user_id, now, now, 1 if blocked else 0),
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

    # The bot's own bookkeeping shares the settings table, under names that start
    # with an underscore so the settings layer knows to leave them alone.
    def set_note(self, key: str, value: str) -> None:
        self.set_override(f"_{key}", value)

    def note(self, key: str) -> str:
        row = self.one("SELECT value FROM settings WHERE key = ?", (f"_{key}",))
        return str(row["value"]) if row else ""

    def clear_note(self, key: str) -> None:
        self.clear_override(f"_{key}")

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

    # -- services ---------------------------------------------------------
    def services(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM services ORDER BY position, name")

    def service(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM services WHERE name = ?", (name,))

    def save_service(self, name: str, **columns: Any) -> None:
        allowed = {
            "base_url",
            "models",
            "vision_models",
            "enabled",
            "position",
            "custom",
            "discovers_free_models",
            "openrouter_extensions",
            "rested_until",
            "last_error",
        }
        fields = {k: v for k, v in columns.items() if k in allowed}
        self.execute(
            "INSERT OR IGNORE INTO services (name, position, added_at) VALUES (?, ?, ?)",
            (name, self._next_service_position(), time.time()),
        )
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.execute(
            f"UPDATE services SET {assignments} WHERE name = ?",  # noqa: S608 - allowlist above
            (*fields.values(), name),
        )

    def _next_service_position(self) -> int:
        row = self.one("SELECT COALESCE(MAX(position), 0) + 1 AS next FROM services")
        return int(row["next"]) if row else 1

    def delete_service(self, name: str) -> None:
        self.execute("DELETE FROM services WHERE name = ?", (name,))
        self.execute("DELETE FROM credentials WHERE service = ?", (name,))

    # -- credentials ------------------------------------------------------
    def credentials(self, service: str | None = None) -> list[sqlite3.Row]:
        if service is None:
            return self.query("SELECT * FROM credentials ORDER BY service, position, id")
        return self.query(
            "SELECT * FROM credentials WHERE service = ? ORDER BY position, id", (service,)
        )

    def credential(self, credential_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM credentials WHERE id = ?", (credential_id,))

    def add_credential(self, service: str, value: bytes, *, label: str = "") -> int:
        row = self.one(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM credentials WHERE service = ?",
            (service,),
        )
        cursor = self.execute(
            """
            INSERT INTO credentials (service, label, value, position, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (service, label, value, int(row["next"]) if row else 0, time.time()),
        )
        return int(cursor.lastrowid or 0)

    def update_credential(self, credential_id: int, **columns: Any) -> None:
        allowed = {
            "label",
            "value",
            "position",
            "enabled",
            "last_used",
            "last_ok",
            "last_error",
            "rested_until",
        }
        fields = {k: v for k, v in columns.items() if k in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.execute(
            f"UPDATE credentials SET {assignments} WHERE id = ?",  # noqa: S608 - allowlist above
            (*fields.values(), credential_id),
        )

    def count_credential_use(self, credential_id: int, *, failed: bool = False) -> None:
        column = "failures" if failed else "requests"
        self.execute(
            f"UPDATE credentials SET {column} = {column} + 1, last_used = ? WHERE id = ?",  # noqa: S608
            (time.time(), credential_id),
        )

    def delete_credential(self, credential_id: int) -> None:
        self.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))

    # -- per-service usage ------------------------------------------------
    def add_service_usage(
        self,
        day: str,
        service: str,
        *,
        requests: int = 0,
        failures: int = 0,
        tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        self.execute(
            """
            INSERT INTO service_usage (day, service, requests, failures, tokens, cost)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, service) DO UPDATE SET
                requests = service_usage.requests + excluded.requests,
                failures = service_usage.failures + excluded.failures,
                tokens   = service_usage.tokens   + excluded.tokens,
                cost     = service_usage.cost     + excluded.cost
            """,
            (day, service, requests, failures, tokens, cost),
        )

    def service_usage(self, day: str) -> dict[str, sqlite3.Row]:
        rows = self.query("SELECT * FROM service_usage WHERE day = ?", (day,))
        return {row["service"]: row for row in rows}

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
