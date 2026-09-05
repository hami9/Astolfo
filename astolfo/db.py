"""The bot's own database: chats, people, settings, secrets and an audit trail.

SQLite because it is one file, needs no server, and survives a restart on a 1 GB
box. The schema is versioned so later releases add to it instead of rewriting it.

What is stored is deliberately narrow: who is in which chat and how active they
are, never a single line of what anyone said.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def today() -> str:
    """The UTC day every daily counter is keyed by."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


SCHEMA_VERSION = 8

# The most outcome rows one day may hold. In free mode the model changes turn to
# turn, so the key space is models x variants x modes and only a ceiling keeps a
# busy day from writing the file full on its own.
MAX_OUTCOME_ROWS = 500

# Thirteen services, and one of them lists four hundred models. A ceiling here
# is what keeps a misbehaving listing from writing the file full; anything past
# it is simply not remembered, which costs a "new" badge and nothing else.
MAX_SEEN_MODELS = 4000

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
-- chats.style is how this chat likes to be talked to, learned as it goes. JSON,
-- and small: one line for the chat and one for each of a dozen people at most.
-- chats.reception is six counters: how many short, medium and long replies were
-- sent here, and how many of each somebody actually answered.
-- Comments live above a table rather than inside it, because SQLite re-parses
-- the body on ALTER TABLE and a trailing comment there is a syntax error.
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
    replies     INTEGER NOT NULL DEFAULT 0,
    daily_limit INTEGER NOT NULL DEFAULT 0,
    mode        TEXT    NOT NULL DEFAULT '',
    dormant     INTEGER NOT NULL DEFAULT 0,
    style       TEXT    NOT NULL DEFAULT '',
    reception   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL DEFAULT '',
    username    TEXT    NOT NULL DEFAULT '',
    first_seen  REAL,
    last_seen   REAL,
    messages    INTEGER NOT NULL DEFAULT 0,
    blocked     INTEGER NOT NULL DEFAULT 0,
    is_master   INTEGER NOT NULL DEFAULT 0,
    daily_limit INTEGER NOT NULL DEFAULT 0
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

-- What a model actually did, as opposed to what it cost. This is the evidence
-- the bot used to throw away: a reply it had to repair, one it had to reject,
-- and whether anybody answered it. Keyed by the prompt variant too, so the same
-- model on two different prompts can be told apart.
CREATE TABLE IF NOT EXISTS outcomes (
    day        TEXT    NOT NULL,
    service    TEXT    NOT NULL DEFAULT '',
    model      TEXT    NOT NULL DEFAULT '',
    variant    TEXT    NOT NULL DEFAULT '',
    mode       TEXT    NOT NULL DEFAULT '',
    calls      INTEGER NOT NULL DEFAULT 0,
    answered   INTEGER NOT NULL DEFAULT 0,
    repaired   INTEGER NOT NULL DEFAULT 0,
    broken     INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost       REAL    NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, service, model, variant, mode)
);

-- Every model id any service has ever listed, and when it first appeared. The
-- catalog itself lives in memory and is rebuilt on each sync; this is only the
-- memory of what was already there, so that "new" means new to this install and
-- not merely new since the last restart. A model that stops being offered ages
-- out with everything else, and counts as new again if it ever comes back.
CREATE TABLE IF NOT EXISTS seen_models (
    service    TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    first_seen REAL,
    last_seen  REAL,
    context    INTEGER NOT NULL DEFAULT 0,
    free       INTEGER NOT NULL DEFAULT 0,
    vision     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (service, model)
);
CREATE INDEX IF NOT EXISTS seen_models_by_age ON seen_models (first_seen DESC);

-- What a model has done to earn its place in the queue. Only the bad news: a
-- model that answered with silence or nonsense, and how many times. Kept because
-- the counters it mirrors live in memory, so every restart used to put the
-- worst-behaved model back at the front of the free pool with a clean sheet.
CREATE TABLE IF NOT EXISTS model_health (
    model        TEXT PRIMARY KEY,
    strikes      INTEGER NOT NULL DEFAULT 0,
    last_bad     REAL,
    rested_until REAL NOT NULL DEFAULT 0
);

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
    "outcomes",
    "seen_models",
    "model_health",
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
        # Rows written today, counted once and kept in memory: the ceiling is
        # checked on every turn and a COUNT(*) per turn would not be.
        self._outcome_rows: dict[str, int] = {}
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
        if current and current < 3:
            # Limits and the reply mode used to be one global setting each.
            self._add_column("chats", "daily_limit", "INTEGER NOT NULL DEFAULT 0")
            self._add_column("chats", "mode", "TEXT NOT NULL DEFAULT ''")
            self._add_column("users", "daily_limit", "INTEGER NOT NULL DEFAULT 0")
        if current and current < 4:
            # Switched off entirely, which is more than muted.
            self._add_column("chats", "dormant", "INTEGER NOT NULL DEFAULT 0")
        if current and current < 5:
            # The learned speaking style, per chat and per person in it.
            self._add_column("chats", "style", "TEXT NOT NULL DEFAULT ''")
        if current and current < 6:
            # Which reply lengths this chat answers.
            self._add_column("chats", "reception", "TEXT NOT NULL DEFAULT ''")
        # v7 adds the outcomes table, which the schema above already creates.
        if current and current < 8:
            # A model retired for twelve hours used to come back with the next
            # restart, because only the strike count was written down.
            self._add_column("model_health", "rested_until", "REAL NOT NULL DEFAULT 0")

        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()
        if current:
            log.info("database schema upgraded from %s to %s", current, SCHEMA_VERSION)

    def _add_column(self, table: str, name: str, spec: str) -> None:
        """Add a column unless it is already there, so re-running is harmless."""
        existing = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
        if name in existing:
            return
        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")  # noqa: S608
        log.info("added %s.%s", table, name)

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

    def size_bytes(self) -> int:
        """What the database costs on disk, write-ahead log included."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                total += os.path.getsize(self.path + suffix)
        return total

    def prune(self, retain_days: int) -> dict[str, int]:
        """Forget what is old enough not to matter, and say what went.

        Nothing here was ever deleted before, so on a small host the audit trail
        and the per-day counters grew for as long as the bot ran. What is kept
        is what somebody chose - a block, a limit, the owner - and what is
        recent enough to still be true.
        """
        if retain_days <= 0:
            return {}
        cutoff = time.time() - retain_days * 86400
        day = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d")
        removed: dict[str, int] = {}

        def drop(name: str, sql: str, args: tuple) -> None:
            count = self.execute(sql, args).rowcount
            if count > 0:
                removed[name] = count

        drop("audit", "DELETE FROM audit WHERE at < ?", (cutoff,))
        drop("service_usage", "DELETE FROM service_usage WHERE day < ?", (day,))
        drop("outcomes", "DELETE FROM outcomes WHERE day < ?", (day,))
        # A model no service has listed for this long is gone. Forgetting it is
        # what lets it count as new again if it ever comes back.
        drop("seen_models", "DELETE FROM seen_models WHERE last_seen < ?", (cutoff,))
        # A model that misbehaved long enough ago deserves another go: hardware,
        # weights and endpoints all change under the same id.
        drop("model_health", "DELETE FROM model_health WHERE last_bad < ?", (cutoff,))
        # Groups the bot was removed from long ago, and everything about them.
        drop(
            "members",
            "DELETE FROM members WHERE chat_id IN"
            " (SELECT chat_id FROM chats WHERE left_at IS NOT NULL AND left_at < ?)",
            (cutoff,),
        )
        drop("chats", "DELETE FROM chats WHERE left_at IS NOT NULL AND left_at < ?", (cutoff,))
        # People who have not been seen since, unless something was decided
        # about them: a block, a limit, or being the owner.
        drop(
            "members",
            "DELETE FROM members WHERE last_seen IS NOT NULL AND last_seen < ?"
            " AND user_id NOT IN (SELECT user_id FROM users"
            " WHERE blocked = 1 OR is_master = 1 OR daily_limit > 0)",
            (cutoff,),
        )
        drop(
            "users",
            "DELETE FROM users WHERE last_seen IS NOT NULL AND last_seen < ?"
            " AND blocked = 0 AND is_master = 0 AND daily_limit = 0"
            " AND user_id NOT IN (SELECT user_id FROM members)",
            (cutoff,),
        )
        if removed:
            # SQLite keeps the freed pages, so the file only shrinks here.
            self.vacuum()
            log.info("pruned %s", ", ".join(f"{n} {k}" for k, n in removed.items()))
        return removed

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
        allowed = {
            "muted", "reply_chance", "forced_mode", "locale", "notes", "title",
            "daily_limit", "mode", "dormant", "style", "reception",
        }
        fields = {k: v for k, v in columns.items() if k in allowed}
        # An empty title means "this state never learned one", not "clear it".
        # Writing it back erased the title Telegram gave us when the bot joined,
        # which is how a named group ended up showing as a bare numeric id.
        if not str(fields.get("title") or "").strip():
            fields.pop("title", None)
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
            SELECT chat_id, notes, reply_chance, forced_mode, muted, title, locale,
                   mode, daily_limit, dormant, style, reception
            FROM chats
            WHERE notes <> '' OR reply_chance IS NOT NULL OR forced_mode IS NOT NULL
               OR muted = 1 OR mode <> '' OR daily_limit > 0 OR dormant = 1
               OR style <> '' OR reception <> ''
            """
        )

    def list_chats(self, *, active_only: bool = True, limit: int = 50) -> list[sqlite3.Row]:
        # The filter is a bound value rather than a piece of assembled SQL, so
        # this query is a constant no matter who calls it.
        return self.query(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM members m WHERE m.chat_id = c.chat_id) AS people,
                   -- A private chat's id is the person's id, so a row saved
                   -- before the name was recorded can still be named.
                   (SELECT u.name FROM users u WHERE u.user_id = c.chat_id) AS person
            FROM chats c
            WHERE ? = 0 OR c.left_at IS NULL
            ORDER BY c.last_seen IS NULL, c.last_seen DESC
            LIMIT ?
            """,
            (1 if active_only else 0, limit),
        )

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.one(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM members m WHERE m.chat_id = c.chat_id) AS people,
                   (SELECT u.name FROM users u WHERE u.user_id = c.chat_id) AS person
            FROM chats c WHERE c.chat_id = ?
            """,
            (chat_id,),
        )

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

    def set_user_limit(self, user_id: int, limit: int) -> None:
        now = time.time()
        self.execute(
            """
            INSERT INTO users (user_id, first_seen, last_seen, daily_limit) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET daily_limit = excluded.daily_limit
            """,
            (user_id, now, now, max(0, limit)),
        )

    def user_limits(self) -> dict[int, int]:
        """Every per-person cap, for the copy the bot keeps in memory.

        Chats need no equivalent: a chat's limit rides along with the rest of its
        settings when the store loads it.
        """
        return {
            int(row["user_id"]): int(row["daily_limit"])
            for row in self.query("SELECT user_id, daily_limit FROM users WHERE daily_limit > 0")
        }

    def set_every_chat(self, **columns: Any) -> int:
        """Apply the same knob to every group at once."""
        allowed = {"daily_limit", "mode", "reply_chance", "muted", "dormant"}
        fields = {k: v for k, v in columns.items() if k in allowed}
        if not fields:
            return 0
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cursor = self.execute(
            f"UPDATE chats SET {assignments} WHERE left_at IS NULL",  # noqa: S608 - allowlist above
            tuple(fields.values()),
        )
        return cursor.rowcount

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

    def dormant_ids(self) -> set[int]:
        """Chats switched off entirely, read once at startup and kept in memory."""
        return {
            int(row["chat_id"])
            for row in self.query("SELECT chat_id FROM chats WHERE dormant = 1")
        }

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

    # -- what a model actually did ----------------------------------------
    def add_outcome(
        self,
        day: str,
        *,
        service: str,
        model: str,
        variant: str = "",
        mode: str = "",
        calls: int = 0,
        answered: int = 0,
        repaired: int = 0,
        broken: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
        latency_ms: int = 0,
    ) -> None:
        """Fold one turn into the counters for its model and prompt.

        Refuses once the day already holds `MAX_OUTCOME_ROWS`. In free mode the
        model changes turn to turn, so without a ceiling a busy day could write
        a row per model per variant per mode and the file would grow on its own.
        """
        if self._outcome_rows.get(day) is None:
            row = self.one("SELECT COUNT(*) AS n FROM outcomes WHERE day = ?", (day,))
            self._outcome_rows = {day: int(row["n"]) if row else 0}
        if self._outcome_rows[day] >= MAX_OUTCOME_ROWS:
            return

        before = self.execute(
            """
            INSERT INTO outcomes (day, service, model, variant, mode, calls, answered,
                                  repaired, broken, prompt_tokens, completion_tokens,
                                  cost, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, service, model, variant, mode) DO UPDATE SET
                calls    = outcomes.calls    + excluded.calls,
                answered = outcomes.answered + excluded.answered,
                repaired = outcomes.repaired + excluded.repaired,
                broken   = outcomes.broken   + excluded.broken,
                prompt_tokens     = outcomes.prompt_tokens     + excluded.prompt_tokens,
                completion_tokens = outcomes.completion_tokens + excluded.completion_tokens,
                cost       = outcomes.cost + excluded.cost,
                -- A running mean, so one slow call does not define the model.
                latency_ms = CASE WHEN outcomes.calls + excluded.calls > 0
                    THEN (outcomes.latency_ms * outcomes.calls
                          + excluded.latency_ms * excluded.calls)
                         / (outcomes.calls + excluded.calls)
                    ELSE excluded.latency_ms END
            """,
            (day, service, model, variant, mode, calls, answered, repaired, broken,
             prompt_tokens, completion_tokens, cost, latency_ms),
        )
        if before.rowcount and before.lastrowid is not None:
            self._outcome_rows[day] = self._outcome_rows.get(day, 0) + 1

    def outcomes(self, day: str | None = None, *, limit: int = 100) -> list[sqlite3.Row]:
        if day is None:
            return self.query(
                "SELECT * FROM outcomes ORDER BY day DESC, calls DESC LIMIT ?", (limit,)
            )
        return self.query(
            "SELECT * FROM outcomes WHERE day = ? ORDER BY calls DESC LIMIT ?", (day, limit)
        )

    # -- what the services have offered before -----------------------------
    def note_models(self, seen: list[tuple[str, str, int, bool, bool]]) -> list[str]:
        """Remember a catalog listing, and say which ids had never been listed.

        One statement per model, in a single transaction, because a sync of every
        service is a few hundred rows at most and happens on a button press.
        """
        if not seen:
            return []
        now = time.time()
        fresh: list[str] = []
        with self._lock:
            held = self.query("SELECT service, model FROM seen_models")
            known = {(row["service"], row["model"]) for row in held}
            room = MAX_SEEN_MODELS - len(known)
            for service, model, context, free, vision in seen:
                if (service, model) not in known:
                    if room <= 0:
                        continue
                    room -= 1
                    fresh.append(model)
                self._db.execute(
                    """
                    INSERT INTO seen_models
                        (service, model, first_seen, last_seen, context, free, vision)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(service, model) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        context   = excluded.context,
                        free      = excluded.free,
                        vision    = excluded.vision
                    """,
                    (service, model, now, now, context, int(free), int(vision)),
                )
            self._db.commit()
        return fresh

    def newest_models(self, limit: int = 20) -> list[sqlite3.Row]:
        """What appeared most recently, newest first."""
        return self.query(
            "SELECT * FROM seen_models ORDER BY first_seen DESC, model LIMIT ?", (limit,)
        )

    # -- how each model has behaved ----------------------------------------
    def note_strike(self, model: str) -> int:
        """Count one more unusable reply from this model, and say how many now."""
        if not model:
            return 0
        self.execute(
            """
            INSERT INTO model_health (model, strikes, last_bad) VALUES (?, 1, ?)
            ON CONFLICT(model) DO UPDATE SET
                strikes  = model_health.strikes + 1,
                last_bad = excluded.last_bad
            """,
            (model, time.time()),
        )
        row = self.one("SELECT strikes FROM model_health WHERE model = ?", (model,))
        return int(row["strikes"]) if row else 1

    def rest_model(self, model: str, seconds: float) -> None:
        """Write down when a model may be asked again.

        Service rests already outlived a restart; model rests did not, so the
        escalating cooldown a broken model earns was undone by the next update -
        and this bot updates often. Only the row `note_strike` has already
        inserted is touched, so a rest is never recorded for a model with no
        record.
        """
        if not model or seconds <= 0:
            return
        self.execute(
            "UPDATE model_health SET rested_until = ? WHERE model = ?",
            (time.time() + seconds, model),
        )

    def model_rests(self) -> dict[str, float]:
        """When each still-resting model may be asked again, as wall clock."""
        return {
            str(row["model"]): float(row["rested_until"])
            for row in self.query(
                "SELECT model, rested_until FROM model_health WHERE rested_until > ?",
                (time.time(),),
            )
        }

    def model_strikes(self) -> dict[str, int]:
        """What each model has already been caught doing, across restarts."""
        return {
            str(row["model"]): int(row["strikes"])
            for row in self.query("SELECT model, strikes FROM model_health")
        }

    def forget_model_health(self) -> None:
        """Give every model a clean sheet again, from the panel."""
        self.execute("DELETE FROM model_health")

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
