"""sqlite state: Slack thread -> Claude session, plus the approval audit log.

Small enough that synchronous sqlite3 under a lock is the right call — every
operation is a single indexed row touch. Calls are wrapped in asyncio.to_thread
by the caller so the event loop never blocks on disk.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .grants import Grant, covered_by

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    channel_id   TEXT    NOT NULL,
    thread_ts    TEXT    NOT NULL,
    session_id   TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    turns        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, thread_ts)
);

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    channel_id      TEXT,
    thread_ts       TEXT,
    message_ts      TEXT,
    session_id      TEXT,
    tool_name       TEXT NOT NULL,
    tool_input_json TEXT,
    tool_use_id     TEXT,
    state           TEXT NOT NULL
        CHECK (state IN ('pending', 'approved', 'denied', 'timeout')),
    requested_at    INTEGER NOT NULL,
    resolved_at     INTEGER,
    resolved_by     TEXT
);

CREATE INDEX IF NOT EXISTS approvals_by_thread
    ON approvals (channel_id, thread_ts);

-- Persistent "always allow" grants. These live on the HOST, in the daemon's
-- database, so the agent can never grant itself anything: it asks, and the answer
-- is computed out here.
CREATE TABLE IF NOT EXISTS grants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name    TEXT    NOT NULL,
    pattern      TEXT    NOT NULL,
    -- 'prefix' | 'exact' | 'any'. See grants.py; the type decides how much a
    -- pattern generalises, which is the whole safety question.
    match_type   TEXT    NOT NULL DEFAULT 'prefix'
        CHECK (match_type IN ('prefix', 'exact', 'any')),
    created_at   INTEGER NOT NULL,
    created_by   TEXT    NOT NULL,
    use_count    INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER,
    UNIQUE (tool_name, pattern, match_type)
);
"""


@dataclass(frozen=True)
class ThreadSession:
    channel_id: str
    thread_ts: str
    session_id: str
    turns: int

    @property
    def is_new(self) -> bool:
        """True when the CLI has not yet created this session on disk.

        Drives --session-id vs --resume. Flips as soon as a run emits its `init`
        event, not when a run completes: see mark_session_created.
        """
        return self.turns == 0


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- thread <-> session -------------------------------------------------

    def find_session(self, channel_id: str, thread_ts: str) -> ThreadSession | None:
        """Look up a thread without creating one.

        Used for in-thread replies: a reply in a thread we don't own must not
        register that thread as ours.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT session_id, turns FROM threads "
                "WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            ).fetchone()
        if row is None:
            return None
        return ThreadSession(channel_id, thread_ts, row["session_id"], row["turns"])

    def get_or_create_session(
        self, channel_id: str, thread_ts: str, new_session_id: str
    ) -> ThreadSession:
        """Return the thread's session, registering new_session_id if it's new.

        The UUID is minted by the caller and stored before the CLI ever runs, so
        the mapping is durable even if the run dies immediately.
        """
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT session_id, turns FROM threads "
                "WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            ).fetchone()

            if row is not None:
                return ThreadSession(
                    channel_id, thread_ts, row["session_id"], row["turns"]
                )

            self._db.execute(
                "INSERT INTO threads "
                "(channel_id, thread_ts, session_id, created_at, last_used_at, turns) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (channel_id, thread_ts, new_session_id, now, now),
            )
            self._db.commit()
            return ThreadSession(channel_id, thread_ts, new_session_id, 0)

    def mark_session_created(self, channel_id: str, thread_ts: str) -> None:
        """Record that the CLI has created this session on disk.

        Call this when the run's `system`/`init` event arrives, NOT when it
        finishes. The session exists from `init` onward, and `claude` rejects
        `--session-id` for an id that already exists ("Session ID ... is already
        in use"). A run that emits init and then dies must therefore switch the
        thread to --resume, or every later message retries --session-id against
        an existing session and the thread is permanently broken.
        """
        with self._lock:
            self._db.execute(
                "UPDATE threads SET turns = turns + 1, last_used_at = ? "
                "WHERE channel_id = ? AND thread_ts = ?",
                (int(time.time()), channel_id, thread_ts),
            )
            self._db.commit()

    # -- grants -------------------------------------------------------------

    def add_grant(
        self, tool_name: str, pattern: str, created_by: str,
        match_type: str = "prefix",
    ) -> int:
        """Create a grant, or return the existing one's id. Idempotent."""
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM grants "
                "WHERE tool_name = ? AND pattern = ? AND match_type = ?",
                (tool_name, pattern, match_type),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            cursor = self._db.execute(
                "INSERT INTO grants "
                "(tool_name, pattern, match_type, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (tool_name, pattern, match_type, now, created_by),
            )
            self._db.commit()
            return int(cursor.lastrowid or 0)

    def list_grants(self) -> list[Grant]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, tool_name, pattern, match_type, created_by, "
                "created_at, use_count FROM grants ORDER BY tool_name, pattern"
            ).fetchall()
        return [
            Grant(
                id=int(r["id"]), tool_name=r["tool_name"], pattern=r["pattern"],
                match_type=r["match_type"], created_by=r["created_by"],
                created_at=int(r["created_at"]), use_count=int(r["use_count"]),
            )
            for r in rows
        ]

    def find_grant(self, tool_name: str, tool_input: object) -> list[Grant] | None:
        """The grants covering this call, if any, counting each use.

        Matching happens in Python, not SQL: it is not a LIKE. A compound command
        needs every segment covered, quote-aware splitting, a word boundary, and a
        refusal to generalise anything containing substitution or redirection. See
        grants.covered_by.
        """
        used = covered_by(self.list_grants(), tool_name, tool_input)
        if not used:
            return None
        now = int(time.time())
        with self._lock:
            for grant in used:
                self._db.execute(
                    "UPDATE grants SET use_count = use_count + 1, "
                    "last_used_at = ? WHERE id = ?",
                    (now, grant.id),
                )
            self._db.commit()
        return used

    def revoke_grant(self, grant_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM grants WHERE id = ?", (grant_id,))
            self._db.commit()
            return cursor.rowcount > 0

    def revoke_all(self) -> int:
        with self._lock:
            cursor = self._db.execute("DELETE FROM grants")
            self._db.commit()
            return cursor.rowcount

    # -- approvals ----------------------------------------------------------

    def open_approval(
        self,
        approval_id: str,
        channel_id: str,
        thread_ts: str,
        session_id: str | None,
        tool_name: str,
        tool_input_json: str,
        tool_use_id: str | None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO approvals (id, channel_id, thread_ts, session_id, "
                "tool_name, tool_input_json, tool_use_id, state, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    approval_id,
                    channel_id,
                    thread_ts,
                    session_id,
                    tool_name,
                    tool_input_json,
                    tool_use_id,
                    int(time.time()),
                ),
            )
            self._db.commit()

    def attach_message_ts(self, approval_id: str, message_ts: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE approvals SET message_ts = ? WHERE id = ?",
                (message_ts, approval_id),
            )
            self._db.commit()

    def resolve_approval(
        self, approval_id: str, state: str, resolved_by: str | None
    ) -> bool:
        """Move a pending approval to a terminal state.

        Returns False if it was already resolved — that makes a double click, or
        a click racing the timeout, a no-op rather than a second verdict.
        """
        if state not in {"approved", "denied", "timeout"}:
            raise ValueError(f"bad approval state: {state}")
        with self._lock:
            cursor = self._db.execute(
                "UPDATE approvals SET state = ?, resolved_at = ?, resolved_by = ? "
                "WHERE id = ? AND state = 'pending'",
                (state, int(time.time()), resolved_by, approval_id),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def approval(self, approval_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
