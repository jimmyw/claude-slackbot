"""Thread modes: the store API, and the migration off `paused_threads`.

The migration is the reason this file exists. `CREATE TABLE IF NOT EXISTS` does
nothing to a database that already has the old shape, and every other test starts
from an empty file — so a migration bug is invisible until it reaches terra. This
builds the RELEASED schema by hand, migrates it by opening a Store on it, and
checks the rows arrived. Same pattern as test_grants.py.

Run:  .venv/bin/python -m tests.test_thread_modes
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from slackagent.store import (
    MODE_ACTIVE,
    MODE_PAUSED,
    MODE_SILENT,
    Store,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def tables(path: Path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        db.close()


def columns(path: Path, table: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    finally:
        db.close()


# The schema as released in commit 1a7737b — threads without last_seen_ts, and
# paused_threads as the only mode. Written out rather than imported, because the
# point is to migrate what is actually deployed.
OLD_SCHEMA = """
CREATE TABLE threads (
    channel_id   TEXT    NOT NULL,
    thread_ts    TEXT    NOT NULL,
    session_id   TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    turns        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, thread_ts)
);
CREATE TABLE paused_threads (
    channel_id TEXT    NOT NULL,
    thread_ts  TEXT    NOT NULL,
    paused_at  INTEGER NOT NULL,
    paused_by  TEXT,
    PRIMARY KEY (channel_id, thread_ts)
);
"""


def test_api() -> None:
    print("\n[1] the store API")
    with tempfile.TemporaryDirectory() as raw:
        store = Store(Path(raw) / "fresh.sqlite3")

        check("a thread with no row is active",
              store.thread_mode("C1", "1.1") == MODE_ACTIVE)
        check("and has no entry", store.thread_mode_entry("C1", "1.1") is None)

        check("set_thread_mode returns the previous mode (active -> silent)",
              store.set_thread_mode("C1", "1.1", MODE_SILENT, "U_OP") == MODE_ACTIVE)
        check("silent -> paused reports silent",
              store.set_thread_mode("C1", "1.1", MODE_PAUSED, "U_OP") == MODE_SILENT)
        check("paused -> silent reports paused, which is what lets |silent say it "
              "loosened a mute",
              store.set_thread_mode("C1", "1.1", MODE_SILENT, "U_OP") == MODE_PAUSED)

        store.note_dropped("C1", "1.1")
        store.note_dropped("C1", "1.1")
        entry = store.thread_mode_entry("C1", "1.1")
        check("the entry carries who and when",
              entry is not None and entry.set_by == "U_OP" and entry.set_at > 0, entry)
        check("dropped messages are counted", entry is not None and entry.dropped == 2,
              entry)

        store.set_thread_mode("C1", "1.1", MODE_SILENT, "U_OP")
        entry = store.thread_mode_entry("C1", "1.1")
        check("re-setting the same mode resets the drop count",
              entry is not None and entry.dropped == 0, entry)

        store.set_thread_mode("C2", "2.2", MODE_PAUSED, "U_OP")
        check("list_thread_modes returns both", len(store.list_thread_modes()) == 2)
        check("filtered by mode returns one",
              [m.thread_ts for m in store.list_thread_modes(MODE_PAUSED)] == ["2.2"],
              store.list_thread_modes(MODE_PAUSED))

        check("active deletes the row",
              store.set_thread_mode("C1", "1.1", MODE_ACTIVE, "U_OP") == MODE_SILENT
              and store.thread_mode("C1", "1.1") == MODE_ACTIVE
              and len(store.list_thread_modes()) == 1)
        check("clearing an already-active thread is a no-op reporting active",
              store.set_thread_mode("C1", "1.1", MODE_ACTIVE, "U_OP") == MODE_ACTIVE)

        raised = False
        try:
            store.set_thread_mode("C1", "1.1", "chatty", "U_OP")
        except ValueError:
            raised = True
        check("an unknown mode raises rather than storing itself", raised)

        print("\n[2] the catch-up watermark")
        check("an unknown thread has no watermark",
              store.last_forwarded_ts("C9", "9.9") is None)
        store.get_or_create_session("C1", "1.1", "11111111-1111-1111-1111-111111111111")
        check("a fresh thread starts with none — which must mean 'start here', "
              "never 'fetch everything'",
              store.last_forwarded_ts("C1", "1.1") is None)

        store.mark_forwarded("C1", "1.1", "1700000200.000100")
        check("it advances", store.last_forwarded_ts("C1", "1.1") == "1700000200.000100")
        store.mark_forwarded("C1", "1.1", "1700000100.000100")
        check("an older ts does NOT move it back",
              store.last_forwarded_ts("C1", "1.1") == "1700000200.000100",
              store.last_forwarded_ts("C1", "1.1"))
        # Slack ts strings are not safely lexicographic; the SQL casts to REAL.
        store.mark_forwarded("C1", "1.1", "999999999.000100")
        check("a shorter, numerically smaller ts does not move it back either",
              store.last_forwarded_ts("C1", "1.1") == "1700000200.000100",
              store.last_forwarded_ts("C1", "1.1"))

        session = store.find_session("C1", "1.1")
        check("marking the watermark leaves `turns` alone — a whole-row write here "
              "would break --session-id vs --resume for ever",
              session is not None and session.turns == 0, session)
        store.close()


def test_migration() -> None:
    print("\n[3] migrating a released database")
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "old.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(OLD_SCHEMA)
        db.execute(
            "INSERT INTO threads (channel_id, thread_ts, session_id, created_at, "
            "last_used_at, turns) VALUES ('C1', '1.1', 'sess-1', 100, 200, 3)"
        )
        db.executescript(
            """
            INSERT INTO paused_threads VALUES ('C1', '1.1', 1700000000, 'U_OP');
            INSERT INTO paused_threads VALUES ('C2', '2.2', 1700000001, NULL);
            """
        )
        db.commit()
        db.close()

        store = Store(path)  # opening IS the migration

        check("paused_threads is gone", "paused_threads" not in tables(path),
              tables(path))
        check("thread_modes exists", "thread_modes" in tables(path))
        check("threads gained last_seen_ts", "last_seen_ts" in columns(path, "threads"))

        modes = {m.thread_ts: m for m in store.list_thread_modes()}
        check("both pauses survived", set(modes) == {"1.1", "2.2"}, sorted(modes))
        check("as mode=paused",
              all(m.mode == MODE_PAUSED for m in modes.values()),
              [(k, v.mode) for k, v in modes.items()])
        check("paused_at became set_at", modes["1.1"].set_at == 1700000000,
              modes["1.1"])
        check("paused_by became set_by", modes["1.1"].set_by == "U_OP", modes["1.1"])
        check("a NULL paused_by does not crash and reads as empty",
              modes["2.2"].set_by == "", modes["2.2"])
        check("oldest first", [m.thread_ts for m in store.list_thread_modes()]
              == ["1.1", "2.2"])

        existing = store.find_session("C1", "1.1")
        check("the existing thread row is untouched",
              existing is not None and existing.session_id == "sess-1"
              and existing.turns == 3, existing)
        check("and its watermark is NULL, so no old thread backfills its history",
              store.last_forwarded_ts("C1", "1.1") is None)

        print("\n[4] the migration is idempotent")
        store.close()
        again = Store(path)
        check("reopening changes nothing", len(again.list_thread_modes()) == 2)
        check("and paused_threads stays gone", "paused_threads" not in tables(path))
        again.close()


def test_interrupted_migration() -> None:
    """The half-migrated shape: both tables present, one row already copied.

    This is the case a `RENAME` implementation would never reach, and the reason the
    copy is INSERT OR IGNORE — a database interrupted between the copy and the drop
    must not fail on the primary key for ever.
    """
    print("\n[5] a migration interrupted between the copy and the drop")
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "half.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(OLD_SCHEMA)
        db.executescript(
            """
            CREATE TABLE thread_modes (
                channel_id TEXT    NOT NULL,
                thread_ts  TEXT    NOT NULL,
                mode       TEXT    NOT NULL CHECK (mode IN ('silent', 'paused')),
                set_at     INTEGER NOT NULL,
                set_by     TEXT,
                dropped    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (channel_id, thread_ts)
            );
            INSERT INTO paused_threads VALUES ('C1', '1.1', 1700000000, 'U_OP');
            INSERT INTO paused_threads VALUES ('C2', '2.2', 1700000001, 'U_OP');
            -- already copied, with a mode that must not be overwritten
            INSERT INTO thread_modes VALUES ('C1', '1.1', 'silent', 1700000009, 'U_OP', 4);
            """
        )
        db.commit()
        db.close()

        store = Store(path)
        modes = {m.thread_ts: m for m in store.list_thread_modes()}
        check("it completes rather than raising on the primary key",
              set(modes) == {"1.1", "2.2"}, sorted(modes))
        check("the already-copied row wins over the legacy one",
              modes["1.1"].mode == MODE_SILENT and modes["1.1"].dropped == 4,
              modes["1.1"])
        check("the uncopied row is carried over",
              modes["2.2"].mode == MODE_PAUSED, modes["2.2"])
        check("and the old table is dropped", "paused_threads" not in tables(path))
        store.close()


def main() -> int:
    test_api()
    test_migration()
    test_interrupted_migration()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
