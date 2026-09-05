"""The bot's own database: what it keeps, and what it must never keep."""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

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


# -- services and credentials --------------------------------------------
def test_a_service_can_hold_more_than_one_key(settings):
    db = open_database(settings.data_dir)
    first = db.add_credential("google", b"cipher-one", label="old")
    second = db.add_credential("google", b"cipher-two", label="new")

    rows = db.credentials("google")
    assert [row["id"] for row in rows] == [first, second], "in the order they were added"
    assert [row["position"] for row in rows] == [0, 1]
    assert db.credential(second)["label"] == "new"


def test_a_key_carries_its_own_state(settings):
    db = open_database(settings.data_dir)
    key = db.add_credential("google", b"cipher")

    db.count_credential_use(key)
    db.count_credential_use(key, failed=True)
    db.update_credential(key, last_error="the key was refused", enabled=0)

    row = db.credential(key)
    assert (row["requests"], row["failures"]) == (1, 1)
    assert row["last_error"] == "the key was refused"
    assert row["enabled"] == 0
    assert row["last_used"] > 0


def test_deleting_a_service_takes_its_keys_with_it(settings):
    db = open_database(settings.data_dir)
    db.save_service("together", base_url="https://api.together.xyz/v1", custom=1)
    db.add_credential("together", b"cipher")

    db.delete_service("together")
    assert db.service("together") is None
    assert db.credentials("together") == []


def test_services_keep_the_order_they_were_given(settings):
    db = open_database(settings.data_dir)
    for name in ("openrouter", "google", "groq"):
        db.save_service(name)
    db.save_service("groq", position=0)

    assert [row["name"] for row in db.services()][0] == "groq"


def test_usage_is_added_up_per_service_per_day(settings):
    db = open_database(settings.data_dir)
    db.add_service_usage("2026-08-18", "google", requests=1, tokens=100, cost=0.001)
    db.add_service_usage("2026-08-18", "google", requests=1, failures=1, tokens=50)
    db.add_service_usage("2026-08-19", "google", requests=1)

    today = db.service_usage("2026-08-18")["google"]
    assert (today["requests"], today["failures"], today["tokens"]) == (2, 1, 150)
    assert today["cost"] == 0.001
    assert db.service_usage("2026-08-19")["google"]["requests"] == 1


def test_keys_saved_under_the_old_scheme_are_carried_over(settings, monkeypatch):
    """An install from before this change must not lose its keys."""
    db = open_database(settings.data_dir)
    db.execute("PRAGMA user_version=1")
    db.set_secret("OPENROUTER_API_KEY", b"cipher-or")
    db.set_secret("GOOGLE_API_KEY", b"cipher-g")
    db.set_secret("SOMETHING_ELSE", b"not-a-provider-key")
    db.close()

    upgraded = open_database(settings.data_dir)
    assert upgraded.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    assert upgraded.credentials("openrouter")[0]["value"] == b"cipher-or"
    assert upgraded.credentials("google")[0]["value"] == b"cipher-g"
    assert upgraded.credentials("something_else") == []


def test_an_older_database_gains_the_reception_column(settings):
    db = open_database(settings.data_dir)
    db.execute("ALTER TABLE chats DROP COLUMN reception")
    db.execute("PRAGMA user_version=5")
    db.seen_chat(-100, kind="supergroup", title="Test Group")
    db.close()

    upgraded = open_database(settings.data_dir)
    assert upgraded.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    upgraded.save_chat_state(-100, reception='{"sent": {"short": 3}}')
    assert upgraded.chat(-100)["reception"] == '{"sent": {"short": 3}}'


def test_an_older_database_gains_the_style_column(settings):
    """A running install upgrades in place; it does not start over."""
    db = open_database(settings.data_dir)
    db.execute("ALTER TABLE chats DROP COLUMN reception")
    db.execute("ALTER TABLE chats DROP COLUMN style")
    db.execute("PRAGMA user_version=4")
    db.seen_chat(-100, kind="supergroup", title="Test Group")
    db.close()

    upgraded = open_database(settings.data_dir)
    assert upgraded.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    assert upgraded.chat(-100)["title"] == "Test Group"

    upgraded.save_chat_state(-100, style='{"chat": "Finglish"}')
    assert upgraded.chat(-100)["style"] == '{"chat": "Finglish"}'
    assert [row["chat_id"] for row in upgraded.chat_settings()] == [-100]


# -- keeping the file from growing forever --------------------------------
LONG_AGO = time.time() - 200 * 86400


@pytest.fixture
def db(settings):
    return open_database(settings.data_dir)


# -- remembering what the services have offered ----------------------------
def listing(*names, service="groq"):
    return [(service, name, 32768, True, False) for name in names]


def test_a_first_listing_is_all_new(db):
    assert db.note_models(listing("qwen3-32b", "llama-4-scout")) == [
        "qwen3-32b",
        "llama-4-scout",
    ]


def test_a_model_is_only_new_once(db):
    db.note_models(listing("qwen3-32b"))
    assert db.note_models(listing("qwen3-32b", "gpt-oss-120b")) == ["gpt-oss-120b"]


def test_being_listed_again_does_not_reset_when_it_first_appeared(db):
    """Otherwise every sync would report the whole catalog as new."""
    db.note_models(listing("qwen3-32b"))
    first = db.newest_models()[0]["first_seen"]
    db.note_models(listing("qwen3-32b"))

    row = db.newest_models()[0]
    assert row["first_seen"] == first
    assert row["last_seen"] >= first


def test_the_same_id_at_two_services_is_two_models(db):
    """They can differ in window and in price, and one can vanish without the other."""
    db.note_models(listing("gpt-oss-120b", service="groq"))
    assert db.note_models(listing("gpt-oss-120b", service="cerebras")) == ["gpt-oss-120b"]
    assert len(db.newest_models()) == 2


def test_a_listing_cannot_write_the_file_full(db):
    from astolfo.db import MAX_SEEN_MODELS

    db.note_models(listing(*(f"model-{n}" for n in range(MAX_SEEN_MODELS + 100))))
    assert len(db.newest_models(limit=MAX_SEEN_MODELS + 200)) == MAX_SEEN_MODELS


def test_a_model_nobody_offers_any_more_is_forgotten(db):
    """And counts as new again if it comes back, which is what it would be."""
    db.note_models(listing("retired", "current"))
    db.execute("UPDATE seen_models SET last_seen = ? WHERE model = 'retired'", (LONG_AGO,))

    assert db.prune(90)["seen_models"] == 1
    assert [row["model"] for row in db.newest_models()] == ["current"]
    assert db.note_models(listing("retired")) == ["retired"]


def test_the_audit_trail_does_not_grow_forever(db):
    db.execute("INSERT INTO audit (at, actor, action) VALUES (?, 1, 'old')", (LONG_AGO,))
    db.record(actor=1, action="recent")

    removed = db.prune(90)

    assert removed["audit"] == 1
    assert [row["action"] for row in db.audit_trail()] == ["recent"]


def test_day_counters_older_than_the_window_go(db):
    db.add_service_usage("2020-01-01", "openrouter", requests=5)
    db.add_service_usage(time.strftime("%Y-%m-%d"), "openrouter", requests=1)

    db.prune(90)

    days = {row["day"] for row in db.query("SELECT day FROM service_usage")}
    assert "2020-01-01" not in days
    assert len(days) == 1


def test_a_group_the_bot_left_long_ago_is_forgotten_with_its_members(db):
    db.joined_chat(-1, kind="supergroup", title="Gone", username="")
    db.seen_member(user_id=7, chat_id=-1, name="someone")
    db.execute("UPDATE chats SET left_at = ? WHERE chat_id = ?", (LONG_AGO, -1))

    db.prune(90)

    assert db.chat(-1) is None
    assert db.query("SELECT * FROM members WHERE chat_id = -1") == []


def test_a_group_it_is_still_in_is_never_touched(db):
    db.joined_chat(-2, kind="supergroup", title="Alive", username="")
    db.seen_member(user_id=7, chat_id=-2, name="someone")

    db.prune(90)

    assert db.chat(-2) is not None
    assert db.query("SELECT * FROM members WHERE chat_id = -2")


def test_a_decision_about_somebody_outlives_the_retention_window(db):
    """A block or a limit was chosen on purpose; forgetting it undoes the choice."""
    for user_id in (11, 22, 33, 44):
        db.seen_member(user_id=user_id, chat_id=-9, name=f"person{user_id}")
    db.execute("DELETE FROM members")  # nobody is in a group any more
    db.set_blocked(11, True)
    db.set_user_limit(22, 50)
    db.execute("UPDATE users SET is_master = 1 WHERE user_id = 33")
    db.execute("UPDATE users SET last_seen = ?", (LONG_AGO,))

    db.prune(90)

    kept = {row["user_id"] for row in db.query("SELECT user_id FROM users")}
    assert kept == {11, 22, 33}, "only the one nobody decided anything about goes"


def test_keeping_everything_is_still_possible(db):
    db.execute("INSERT INTO audit (at, actor, action) VALUES (?, 1, 'old')", (LONG_AGO,))
    assert db.prune(0) == {}
    assert db.audit_trail()


def test_pruning_reports_what_it_removed(db):
    db.execute("INSERT INTO audit (at, actor, action) VALUES (?, 1, 'a')", (LONG_AGO,))
    db.execute("INSERT INTO audit (at, actor, action) VALUES (?, 1, 'b')", (LONG_AGO,))
    assert db.prune(90) == {"audit": 2}


def test_the_size_on_disk_is_reported(db):
    assert db.size_bytes() > 0


def test_saving_state_never_erases_a_title_it_does_not_know(db):
    """The in-memory state starts with no title; writing it back wiped the real one.

    That is how a named group came to show as a bare numeric id in the panel.
    """
    db.joined_chat(-77, kind="supergroup", title="Weekend Plans", username="weekend")

    db.save_chat_state(-77, muted=1, title="")

    assert db.chat(-77)["title"] == "Weekend Plans"
    assert db.chat(-77)["muted"] == 1


def test_a_title_that_is_actually_known_still_updates(db):
    db.joined_chat(-78, kind="supergroup", title="Old Name", username="")
    db.save_chat_state(-78, title="New Name")
    assert db.chat(-78)["title"] == "New Name"


def test_a_private_chat_can_be_named_from_the_person(db):
    """A private chat's id is the person's id, so an old row is still nameable."""
    db.seen_member(user_id=42, chat_id=42, name="Reza")
    db.seen_chat(42, kind="private")

    assert db.chat(42)["person"] == "Reza"
    assert [row["person"] for row in db.list_chats() if row["chat_id"] == 42] == ["Reza"]
