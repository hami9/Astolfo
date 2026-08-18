"""The bot's own database: what it keeps, and what it must never keep."""

from __future__ import annotations

import json
import os
import stat

from astolfo.db import SCHEMA_VERSION, open_database
from astolfo.memory import ChatStore


def test_the_database_is_not_world_readable(settings):
    db = open_database(settings.data_dir)
    mode = stat.S_IMODE(os.stat(db.path).st_mode)
    assert mode & 0o077 == 0, "anyone else on the box could read the keys"


def test_reopening_keeps_the_schema_version(settings):
    open_database(settings.data_dir).close()
    db = open_database(settings.data_dir)
    assert db.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION


def test_activity_is_counted_per_chat_and_per_person(settings):
    db = open_database(settings.data_dir)
    for _ in range(3):
        db.seen_chat(-100, kind="supergroup", title="Test Group")
        db.seen_member(user_id=7, chat_id=-100, name="Reza", username="reza")
    db.seen_member(user_id=8, chat_id=-100, name="Sara")

    chat = db.chat(-100)
    assert chat["title"] == "Test Group"
    assert chat["messages"] == 3
    people = {row["name"]: row["messages"] for row in db.members(-100)}
    assert people == {"Reza": 3, "Sara": 1}
    assert db.user(7)["username"] == "reza"


def test_a_missing_username_does_not_erase_the_known_one(settings):
    """Telegram omits the username on some updates; absence is not a change."""
    db = open_database(settings.data_dir)
    db.seen_member(user_id=7, chat_id=-100, name="Reza", username="reza")
    db.seen_member(user_id=7, chat_id=-100, name="Reza")
    assert db.user(7)["username"] == "reza"


def test_leaving_and_rejoining_a_chat(settings):
    db = open_database(settings.data_dir)
    db.joined_chat(-100, kind="supergroup", title="Test Group")
    db.left_chat(-100)
    assert [row["chat_id"] for row in db.list_chats()] == []

    db.joined_chat(-100, kind="supergroup", title="Test Group")
    assert [row["chat_id"] for row in db.list_chats()] == [-100]
    assert db.chat(-100)["left_at"] is None


def test_settings_and_secrets_round_trip(settings):
    db = open_database(settings.data_dir)
    db.set_override("free_mode", "1", by=42)
    db.set_secret("OPENROUTER_API_KEY", b"cipher", by=42)

    assert db.overrides() == {"free_mode": "1"}
    assert db.secret("OPENROUTER_API_KEY") == b"cipher"
    assert [row["name"] for row in db.secret_names()] == ["OPENROUTER_API_KEY"]

    db.clear_override("free_mode")
    db.delete_secret("OPENROUTER_API_KEY")
    assert db.overrides() == {}
    assert db.secret("OPENROUTER_API_KEY") is None


def test_only_one_master_at_a_time(settings):
    db = open_database(settings.data_dir)
    db.claim_master(1, name="First")
    db.claim_master(2, name="Second")
    assert db.master_id() == 2


def test_blocking_is_remembered(settings):
    db = open_database(settings.data_dir)
    db.seen_member(user_id=9, chat_id=-100, name="Spammer")
    db.set_blocked(9, True)
    assert db.blocked_ids() == {9}
    db.set_blocked(9, False)
    assert db.blocked_ids() == set()


def test_the_audit_trail_is_newest_first(settings):
    db = open_database(settings.data_dir)
    db.record(actor=1, action="set_key", detail="openrouter")
    db.record(actor=1, action="restart")
    assert [row["action"] for row in db.audit_trail()] == ["restart", "set_key"]


# -- migration ------------------------------------------------------------
def test_an_old_state_file_is_imported_once(settings):
    legacy = {
        "chats": {
            "101": {
                "notes": "Reza loves coffee",
                "reply_chance": 0.5,
                "forced_mode": "think",
                "muted": True,
                "title": "Old Group",
                "locale": "fa",
            }
        }
    }
    path = os.path.join(settings.data_dir, "state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    state = ChatStore(settings, open_database(settings.data_dir)).get(101)
    assert state.notes == "Reza loves coffee"
    assert state.reply_chance == 0.5
    assert state.forced_mode == "think"
    assert state.muted is True

    # The file stays where it is, but a second start must not undo later edits.
    store = ChatStore(settings, open_database(settings.data_dir))
    store.get(101).notes = "moved on"
    store.mark_dirty()
    store.save()
    again = ChatStore(settings, open_database(settings.data_dir))
    assert again.get(101).notes == "moved on"


def test_message_text_never_reaches_the_database(settings):
    db = open_database(settings.data_dir)
    store = ChatStore(settings, db)
    state = store.get(101)
    state.add_user("reza", "my credit card is 4111 1111 1111 1111")
    db.seen_member(user_id=7, chat_id=101, name="reza")
    store.mark_dirty()
    store.save()

    # The write-ahead log holds recent writes too, so check everything on disk.
    for name in os.listdir(settings.data_dir):
        if not name.startswith("astolfo.db"):
            continue
        with open(os.path.join(settings.data_dir, name), "rb") as fh:
            assert b"4111" not in fh.read(), f"message text leaked into {name}"
