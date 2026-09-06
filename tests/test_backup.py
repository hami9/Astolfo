"""The backup button has to hand over the database, not most of it.

The database runs in WAL mode, so a commit lands in `astolfo.db-wal` and reaches
`astolfo.db` only at a checkpoint. Handing over the main file alone therefore
hands over a database missing everything since the last one - on the live box
that was thirty-one calls of evidence behind a 4 MB log. No test covered the
button, which is how it survived.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from types import SimpleNamespace

from astolfo.admin import sections
from astolfo.admin.panel import Ctx
from astolfo.db import Database


def _rows(path: str, table: str = "audit") -> int:
    """Read a file the way somebody restoring the backup would: on its own.

    -1 when the table is not even there, which is what a copy taken before the
    first checkpoint looks like: the schema is in the log too.
    """
    db = sqlite3.connect(path)
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return -1
    finally:
        db.close()


def _busy(db: Database, count: int = 40) -> None:
    """Commit enough to fill the write-ahead log without checkpointing it."""
    for index in range(count):
        db.record(actor=index, action="test", detail=f"row {index}")


def test_a_copy_of_the_main_file_alone_is_behind(tmp_path):
    """The defect itself, so the fix below is measured against something real."""
    db = Database(str(tmp_path / "astolfo.db"))
    _busy(db)

    stale = str(tmp_path / "stale.db")
    shutil.copyfile(db.path, stale)

    assert _rows(stale) < db.counts()["audit"], "the WAL should still hold the newest rows"
    db.close()


def test_the_snapshot_holds_everything_the_live_database_does(tmp_path):
    db = Database(str(tmp_path / "astolfo.db"))
    _busy(db)

    dest = str(tmp_path / "snap.db")
    db.snapshot(dest)

    assert _rows(dest) == db.counts()["audit"]
    db.close()


def test_the_snapshot_is_a_whole_database_on_its_own(tmp_path):
    db = Database(str(tmp_path / "astolfo.db"))
    _busy(db)
    dest = str(tmp_path / "snap.db")
    db.snapshot(dest)
    db.close()

    # No sidecars: a restore is one file, and it opens at the current schema.
    assert not os.path.exists(dest + "-wal")
    copy = sqlite3.connect(dest)
    assert copy.execute("PRAGMA user_version").fetchone()[0] > 0
    copy.close()


def test_the_snapshot_is_not_world_readable(tmp_path):
    """It carries the encrypted credentials, so it is born as tight as the original."""
    db = Database(str(tmp_path / "astolfo.db"))
    dest = str(tmp_path / "snap.db")
    db.snapshot(dest)

    assert os.stat(dest).st_mode & 0o077 == 0
    db.close()


def test_the_button_sends_a_snapshot_and_not_the_live_file(rt):
    _busy(rt.db)
    ctx = Ctx(rt=rt, user=SimpleNamespace(id=1, full_name="owner"), bot=None)

    view = sections.backup(ctx)

    assert view.document != rt.db.path, "the live file was handed over"
    assert view.extras.get("temporary"), "a temporary copy has to be cleaned up after sending"
    assert os.path.exists(view.document)
    assert _rows(view.document) == rt.db.counts()["audit"]


async def test_the_temporary_copy_does_not_outlive_the_send(rt, bot):
    from astolfo.admin import panel
    from tests.conftest import FakeContext, FakeMessage

    ctx = Ctx(rt=rt, user=SimpleNamespace(id=1, full_name="owner"), bot=bot)
    view = sections.backup(ctx)
    folder = os.path.dirname(view.document)

    await panel._send_document(view, FakeMessage(text=""), FakeContext(rt, bot))

    assert bot.documents == [os.path.basename(view.document)]
    assert not os.path.exists(folder), "a copy of the database was left on disk"


async def test_a_failed_send_leaves_no_copy_behind(rt, bot):
    from astolfo.admin import panel
    from tests.conftest import FakeContext, FakeMessage

    async def explode(*args, **kwargs):
        raise RuntimeError("telegram said no")

    bot.send_document = explode
    ctx = Ctx(rt=rt, user=SimpleNamespace(id=1, full_name="owner"), bot=bot)
    view = sections.backup(ctx)
    folder = os.path.dirname(view.document)

    await panel._send_document(view, FakeMessage(text=""), FakeContext(rt, bot))

    assert not os.path.exists(folder), "the failed send left the database on disk"
