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
    -- High-water mark: the ts of the newest message forwarded to Claude from this
    -- thread. Drives the catch-up transcript. NULL means "start here" — never
    -- "fetch everything", or the first mention in an old thread would drag its
    -- whole history into the prompt.
    last_seen_ts TEXT,
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
    -- Who asked. Distinct from resolved_by, which is who decided: with guests able
    -- to talk to the bot, an audit row naming only the approver loses the fact that
    -- someone else requested the action.
    requested_by    TEXT,
    state           TEXT NOT NULL
        CHECK (state IN ('pending', 'approved', 'denied', 'timeout')),
    requested_at    INTEGER NOT NULL,
    resolved_at     INTEGER,
    resolved_by     TEXT
);

CREATE INDEX IF NOT EXISTS approvals_by_thread
    ON approvals (channel_id, thread_ts);

-- Runtime settings the operator can change from Slack, so a policy switch does
-- not need an .env edit and a restart. The .env value remains the default for a
-- key that has never been set.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT
);

-- Threads the operator has taken out of the default mode with |silent or |pause.
-- A separate table rather than a column on `threads`, for two reasons: a new table
-- needs no migration (CREATE TABLE IF NOT EXISTS does create one that is absent,
-- unlike a column), and a thread can be muted before the bot has ever run in it.
--
-- 'active' is the ABSENCE of a row, so |resume is a DELETE and a fresh thread costs
-- no write. The CHECK makes 'active' unstorable, so "a row exists" can only ever
-- mean muted — a future upsert cannot silently mute every thread it touches.
CREATE TABLE IF NOT EXISTS thread_modes (
    channel_id TEXT    NOT NULL,
    thread_ts  TEXT    NOT NULL,
    -- 'silent': forward only when explicitly addressed (a mention or a DM).
    -- 'paused': forward nothing at all.
    mode       TEXT    NOT NULL CHECK (mode IN ('silent', 'paused')),
    set_at     INTEGER NOT NULL,
    set_by     TEXT,
    -- Messages dropped since the mode was set. Without a count, "why is it quiet"
    -- is unanswerable without the daemon log, which is not in Slack.
    dropped    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, thread_ts)
);

-- MCP: the audit trail the host-side proxy exists for. One row per tool call the
-- guest attempted, decided out here where the agent cannot reach it. slack_user is
-- resolved from the run token on the host, NOT from anything the guest sent, so it
-- names a person the guest could not have lied about.
CREATE TABLE IF NOT EXISTS mcp_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at   INTEGER NOT NULL,
    slack_user  TEXT,
    channel_id  TEXT,
    thread_ts   TEXT,
    session_id  TEXT,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    decision    TEXT    NOT NULL
        CHECK (decision IN ('allowed', 'denied', 'capped', 'error')),
    reason      TEXT,
    args_digest TEXT,
    result_bytes INTEGER,
    duration_ms INTEGER
);

CREATE INDEX IF NOT EXISTS mcp_calls_recent ON mcp_calls (called_at);

-- OAuth grants for MCP upstreams, per (server, slack_user). slack_user '' is the
-- shared grant. These live here rather than in the config file because refresh
-- tokens ROTATE: the first refresh would otherwise invalidate the value in the
-- file and silently brick the credential.
CREATE TABLE IF NOT EXISTS mcp_tokens (
    server        TEXT    NOT NULL,
    slack_user    TEXT    NOT NULL DEFAULT '',
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    INTEGER,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (server, slack_user)
);

-- Runtime policy changes from |mcp, so allowing a newly discovered tool needs no
-- file edit and no restart. Credentials are NEVER stored here — only patterns.
-- slack_user '' means everyone; a per-user row wins over it.
CREATE TABLE IF NOT EXISTS mcp_policy (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    server     TEXT    NOT NULL,
    slack_user TEXT    NOT NULL DEFAULT '',
    effect     TEXT    NOT NULL CHECK (effect IN ('allow', 'deny')),
    pattern    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    created_by TEXT    NOT NULL,
    UNIQUE (server, slack_user, effect, pattern)
);

-- Servers switched off from Slack. Absence means enabled, like thread_modes:
-- enabling is a DELETE and a newly configured server needs no write to be live.
CREATE TABLE IF NOT EXISTS mcp_disabled (
    server      TEXT    PRIMARY KEY,
    disabled_at INTEGER NOT NULL,
    disabled_by TEXT
);

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


# The three per-thread reply modes. 'active' is never stored — it is the absence
# of a thread_modes row — but it is the value thread_mode() reports, so it belongs
# here beside the others.
MODE_ACTIVE = "active"
MODE_SILENT = "silent"
MODE_PAUSED = "paused"
THREAD_MODES = (MODE_ACTIVE, MODE_SILENT, MODE_PAUSED)


@dataclass(frozen=True)
class ThreadMode:
    channel_id: str
    thread_ts: str
    mode: str
    set_at: int
    set_by: str
    dropped: int


def _thread_mode(row: sqlite3.Row) -> ThreadMode:
    return ThreadMode(
        channel_id=row["channel_id"],
        thread_ts=row["thread_ts"],
        mode=row["mode"],
        set_at=int(row["set_at"]),
        set_by=row["set_by"] or "",
        dropped=int(row["dropped"]),
    )


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
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Bring an existing database up to the current schema.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
        so every column added after a release needs an explicit migration.
        Without this the schema in SCHEMA is only true of *fresh* databases, and
        the first query touching a new column fails at runtime — which is exactly
        what happened: `match_type` broke `|status`, and `requested_by` would have
        crashed the next approval request and with it the gate.
        """
        def columns(table: str) -> set[str]:
            return {
                row["name"]
                for row in self._db.execute(f"PRAGMA table_info({table})")
            }

        def tables() -> set[str]:
            # PRAGMA table_info on a table that does not exist returns an empty
            # set rather than raising, so columns() cannot tell "absent" from "has
            # no columns". Anything table-level has to ask sqlite_master.
            return {
                row["name"]
                for row in self._db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        # approvals.requested_by — a plain add, no constraint change.
        if "requested_by" not in columns("approvals"):
            self._db.execute("ALTER TABLE approvals ADD COLUMN requested_by TEXT")

        # grants.match_type — needs a rebuild, not an ALTER: the UNIQUE constraint
        # gained match_type, and sqlite cannot alter a constraint in place. With the
        # old two-column UNIQUE still in force, adding an `exact` grant beside an
        # existing `prefix` one for the same pattern would raise IntegrityError.
        if "match_type" not in columns("grants"):
            self._db.executescript(
                """
                CREATE TABLE grants_migrated (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name    TEXT    NOT NULL,
                    pattern      TEXT    NOT NULL,
                    match_type   TEXT    NOT NULL DEFAULT 'prefix'
                        CHECK (match_type IN ('prefix', 'exact', 'any')),
                    created_at   INTEGER NOT NULL,
                    created_by   TEXT    NOT NULL,
                    use_count    INTEGER NOT NULL DEFAULT 0,
                    last_used_at INTEGER,
                    UNIQUE (tool_name, pattern, match_type)
                );
                INSERT INTO grants_migrated
                    (id, tool_name, pattern, match_type, created_at, created_by,
                     use_count, last_used_at)
                SELECT id, tool_name, pattern,
                       -- Before match_type existed, '*' was itself the wildcard
                       -- marker for tools with nothing to scope by. Labelling
                       -- those 'prefix' would leave them matching the literal
                       -- string '*', i.e. nothing: the operator's existing grants
                       -- would silently stop working and start asking again.
                       CASE WHEN pattern = '*' THEN 'any' ELSE 'prefix' END,
                       created_at, created_by, use_count, last_used_at
                FROM grants;
                DROP TABLE grants;
                ALTER TABLE grants_migrated RENAME TO grants;
                """
            )

        # threads.last_seen_ts — a plain add, no constraint change.
        if "last_seen_ts" not in columns("threads"):
            self._db.execute("ALTER TABLE threads ADD COLUMN last_seen_ts TEXT")

        # paused_threads -> thread_modes, when |pause was the only mode.
        #
        # NOT a rename. executescript(SCHEMA) runs before this method, so
        # thread_modes already exists (empty) by now and
        #   ALTER TABLE paused_threads RENAME TO thread_modes
        # fails with "there is already another table or index with this name".
        # Copy and drop, the same shape as the grants rebuild above.
        #
        # INSERT OR IGNORE, not INSERT: a database interrupted between the copy and
        # the drop would otherwise fail on the primary key for ever. Every existing
        # row meant exactly one thing — paused — so unlike the grants migration
        # there is no legacy marker to misread.
        if "paused_threads" in tables():
            self._db.executescript(
                """
                INSERT OR IGNORE INTO thread_modes
                    (channel_id, thread_ts, mode, set_at, set_by)
                SELECT channel_id, thread_ts, 'paused', paused_at, paused_by
                FROM paused_threads;
                DROP TABLE paused_threads;
                """
            )

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

    # -- thread modes -------------------------------------------------------

    def thread_mode(self, channel_id: str, thread_ts: str) -> str:
        """The thread's mode. MODE_ACTIVE when there is no row."""
        with self._lock:
            row = self._db.execute(
                "SELECT mode FROM thread_modes "
                "WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            ).fetchone()
        return row["mode"] if row is not None else MODE_ACTIVE

    def thread_mode_entry(
        self, channel_id: str, thread_ts: str
    ) -> ThreadMode | None:
        """The full row, or None when the thread is active.

        Separate from thread_mode() because the commands need to say who set the
        mode and when, while the hot path on every message needs only the word.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT channel_id, thread_ts, mode, set_at, set_by, dropped "
                "FROM thread_modes WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            ).fetchone()
        return _thread_mode(row) if row is not None else None

    def set_thread_mode(
        self, channel_id: str, thread_ts: str, mode: str, set_by: str
    ) -> str:
        """Set the mode and return the PREVIOUS one.

        Returning the previous mode is load-bearing: |silent, |pause and |resume all
        have to tell the operator what actually changed, and |silent typed in a
        paused thread LOOSENS the mute — announcing that is the difference between a
        clear command and a surprise. Read-modify-write in one locked call so the
        answer cannot be stale.
        """
        if mode not in THREAD_MODES:
            raise ValueError(f"unknown thread mode: {mode!r}")
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT mode FROM thread_modes "
                "WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            ).fetchone()
            previous = row["mode"] if row is not None else MODE_ACTIVE

            if mode == MODE_ACTIVE:
                self._db.execute(
                    "DELETE FROM thread_modes "
                    "WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
            else:
                # dropped resets with the mode: the count answers "how much have I
                # missed since I silenced this", not "ever".
                self._db.execute(
                    "INSERT INTO thread_modes "
                    "(channel_id, thread_ts, mode, set_at, set_by, dropped) "
                    "VALUES (?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(channel_id, thread_ts) DO UPDATE SET "
                    "mode = excluded.mode, set_at = excluded.set_at, "
                    "set_by = excluded.set_by, dropped = 0",
                    (channel_id, thread_ts, mode, now, set_by),
                )
            self._db.commit()
            return previous

    def note_dropped(self, channel_id: str, thread_ts: str) -> None:
        """Count one message the mode kept from Claude."""
        with self._lock:
            self._db.execute(
                "UPDATE thread_modes SET dropped = dropped + 1 "
                "WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            )
            self._db.commit()

    def list_thread_modes(self, mode: str | None = None) -> list[ThreadMode]:
        """Every non-active thread, oldest first, optionally one mode only."""
        sql = (
            "SELECT channel_id, thread_ts, mode, set_at, set_by, dropped "
            "FROM thread_modes"
        )
        params: tuple[str, ...] = ()
        if mode is not None:
            sql += " WHERE mode = ?"
            params = (mode,)
        sql += " ORDER BY set_at"
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [_thread_mode(row) for row in rows]

    # -- the catch-up watermark ---------------------------------------------

    def mark_forwarded(self, channel_id: str, thread_ts: str, message_ts: str) -> None:
        """Record that message_ts was forwarded to Claude.

        Monotonic, so a slow turn finishing after a fast one cannot rewind the mark
        and cause the next mention to replay messages the agent has already seen.

        CAST to REAL rather than comparing the strings: Slack timestamps are not
        safely lexicographic ('999999999.000100' < '1724000000.000001' is False).
        Ten-digit epochs hold until 2286, but the cast is free.

        Deliberately a targeted UPDATE of one column. Reading the row and writing it
        back whole would reset `turns`, and turns == 0 is what selects --session-id
        over --resume — that is a permanently broken thread, from an innocuous
        refactor.
        """
        with self._lock:
            self._db.execute(
                "UPDATE threads SET last_seen_ts = ? "
                "WHERE channel_id = ? AND thread_ts = ? AND ("
                "    last_seen_ts IS NULL"
                "    OR CAST(last_seen_ts AS REAL) < CAST(? AS REAL))",
                (message_ts, channel_id, thread_ts, message_ts),
            )
            self._db.commit()

    def last_forwarded_ts(self, channel_id: str, thread_ts: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT last_seen_ts FROM threads "
                "WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            ).fetchone()
        return row["last_seen_ts"] if row is not None else None

    # -- settings -----------------------------------------------------------

    def get_setting(self, key: str, default: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value: str, updated_by: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (key, value, int(time.time()), updated_by),
            )
            self._db.commit()

    def setting_meta(self, key: str) -> tuple[int, str] | None:
        """(updated_at, updated_by) for a setting, or None if never set."""
        with self._lock:
            row = self._db.execute(
                "SELECT updated_at, updated_by FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return (int(row["updated_at"]), row["updated_by"] or "") if row else None

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

    # -- MCP: audit, tokens, runtime policy ---------------------------------

    def record_mcp_call(
        self,
        *,
        slack_user: str,
        channel_id: str,
        thread_ts: str,
        session_id: str,
        server: str,
        tool: str,
        decision: str,
        reason: str = "",
        args_digest: str = "",
        result_bytes: int = 0,
        duration_ms: int = 0,
    ) -> None:
        """Write one attempted MCP call to the audit trail.

        Every attempt, not only the successful ones: a denied call is the more
        interesting row, because it says what the agent wanted.
        """
        if decision not in {"allowed", "denied", "capped", "error"}:
            raise ValueError(f"bad mcp decision: {decision!r}")
        with self._lock:
            self._db.execute(
                "INSERT INTO mcp_calls (called_at, slack_user, channel_id, "
                "thread_ts, session_id, server, tool, decision, reason, "
                "args_digest, result_bytes, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()), slack_user, channel_id, thread_ts, session_id,
                    server, tool, decision, reason, args_digest, result_bytes,
                    duration_ms,
                ),
            )
            self._db.commit()

    def recent_mcp_calls(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM mcp_calls ORDER BY called_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def mcp_call_summary(self, since: int) -> list[sqlite3.Row]:
        """(server, tool, decision, n) since a timestamp, for |mcp."""
        with self._lock:
            return self._db.execute(
                "SELECT server, tool, decision, COUNT(*) AS n FROM mcp_calls "
                "WHERE called_at >= ? GROUP BY server, tool, decision "
                "ORDER BY n DESC",
                (since,),
            ).fetchall()

    def mcp_token(self, server: str, slack_user: str = "") -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM mcp_tokens WHERE server = ? AND slack_user = ?",
                (server, slack_user),
            ).fetchone()

    def save_mcp_token(
        self,
        server: str,
        slack_user: str,
        *,
        access_token: str,
        refresh_token: str,
        expires_at: int,
    ) -> None:
        """Persist a grant after a refresh, rotated refresh token included."""
        with self._lock:
            self._db.execute(
                "INSERT INTO mcp_tokens (server, slack_user, access_token, "
                "refresh_token, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(server, slack_user) DO UPDATE SET "
                "access_token = excluded.access_token, "
                "refresh_token = excluded.refresh_token, "
                "expires_at = excluded.expires_at, "
                "updated_at = excluded.updated_at",
                (
                    server, slack_user, access_token, refresh_token, expires_at,
                    int(time.time()),
                ),
            )
            self._db.commit()

    def add_mcp_policy(
        self, server: str, effect: str, pattern: str, created_by: str,
        slack_user: str = "",
    ) -> int:
        """Add a runtime allow/deny pattern. Idempotent."""
        if effect not in {"allow", "deny"}:
            raise ValueError(f"bad effect: {effect!r}")
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM mcp_policy WHERE server = ? AND slack_user = ? "
                "AND effect = ? AND pattern = ?",
                (server, slack_user, effect, pattern),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            cursor = self._db.execute(
                "INSERT INTO mcp_policy (server, slack_user, effect, pattern, "
                "created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (server, slack_user, effect, pattern, now, created_by),
            )
            self._db.commit()
            return int(cursor.lastrowid or 0)

    def mcp_policy(self, server: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mcp_policy"
        params: tuple[str, ...] = ()
        if server is not None:
            sql += " WHERE server = ?"
            params = (server,)
        sql += " ORDER BY server, slack_user, effect, pattern"
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def remove_mcp_policy(self, policy_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM mcp_policy WHERE id = ?", (policy_id,)
            )
            self._db.commit()
            return cursor.rowcount > 0

    def mcp_disabled(self) -> set[str]:
        with self._lock:
            return {
                row["server"]
                for row in self._db.execute("SELECT server FROM mcp_disabled")
            }

    def set_mcp_enabled(self, server: str, enabled: bool, by: str = "") -> bool:
        """Enable or disable a server. Returns True if this changed anything."""
        with self._lock:
            if enabled:
                cursor = self._db.execute(
                    "DELETE FROM mcp_disabled WHERE server = ?", (server,)
                )
                self._db.commit()
                return cursor.rowcount > 0
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO mcp_disabled "
                "(server, disabled_at, disabled_by) VALUES (?, ?, ?)",
                (server, int(time.time()), by),
            )
            self._db.commit()
            return cursor.rowcount > 0

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
        requested_by: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO approvals (id, channel_id, thread_ts, session_id, "
                "tool_name, tool_input_json, tool_use_id, requested_by, state, "
                "requested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    approval_id,
                    channel_id,
                    thread_ts,
                    session_id,
                    tool_name,
                    tool_input_json,
                    tool_use_id,
                    requested_by,
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
