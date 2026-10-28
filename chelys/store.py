"""Conversation history in SQLite.

Rows are saved as a turn happens (Hermes's persist-as-you-go rule): the user message when
the turn starts, an assistant tool-call row before its tools run, each result as it
arrives. If the phone dies mid-turn, the history still shows what was asked and what ran;
the agent closes such an open turn the next time it runs (see agent._close_open_turn).

Compaction soft-archives rows (active = 0) instead of deleting them. A per-session lease
keeps two Chelys processes (say, the terminal and the Telegram bridge) from interleaving
turns in one conversation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    title TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    prompt INTEGER NOT NULL DEFAULT 0,
    completion INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    cache_miss INTEGER NOT NULL DEFAULT 0
);
-- Skills (see skill_tools.py). Every skill change is a revision, so any change can be undone.
CREATE TABLE IF NOT EXISTS skill_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    skill TEXT NOT NULL,
    file TEXT NOT NULL,          -- path inside the skill, or '' for the whole skill (archive)
    actor TEXT NOT NULL,         -- foreground, review, learn, owner, curator
    action TEXT NOT NULL,
    before TEXT,                 -- NULL: the file didn't exist
    after TEXT,                  -- NULL: the file was removed; for archive, where it went
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS skill_revisions_skill ON skill_revisions(skill, id);
-- Changes the background review proposed, waiting for the owner.
CREATE TABLE IF NOT EXISTS skill_pending (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session_id INTEGER,
    origin TEXT NOT NULL,
    skill TEXT NOT NULL,
    gist TEXT NOT NULL,
    payload TEXT NOT NULL
);
-- Standing approvals: skill '*' with a session id is "this conversation"; a skill name with
-- session 0 is "always for this skill".
CREATE TABLE IF NOT EXISTS skill_grants (
    skill TEXT NOT NULL,
    session_id INTEGER NOT NULL,
    PRIMARY KEY (skill, session_id)
);
-- Ordinary tool approvals are deliberately separate from skill grants.
CREATE TABLE IF NOT EXISTS tool_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_key TEXT NOT NULL,
    session_id INTEGER NOT NULL,
    tool TEXT NOT NULL,
    UNIQUE(action_key, session_id)
);
CREATE TABLE IF NOT EXISTS skill_usage (
    name TEXT PRIMARY KEY,
    created_by TEXT,             -- agent (background review), learn (foreground), NULL: hand-made
    created REAL,
    use_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL DEFAULT 0,
    patch_count INTEGER NOT NULL DEFAULT 0,
    last_used REAL,
    last_viewed REAL,
    last_patched REAL,
    state TEXT NOT NULL DEFAULT 'active',
    pinned INTEGER NOT NULL DEFAULT 0,
    archived_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after v0.1.0; existing databases get them on open.
MIGRATIONS = {
    "messages": [("active", "INTEGER NOT NULL DEFAULT 1"), ("ts", "REAL")],
    "sessions": [("lease_holder", "TEXT"), ("lease_expires", "REAL NOT NULL DEFAULT 0"),
                 ("compact_retry_at", "REAL NOT NULL DEFAULT 0"),
                 # skills: the index frozen into this conversation's prompt, the last change
                 # note sent, and model steps since the last skill review
                 ("skills_index", "TEXT"), ("skills_noted", "TEXT NOT NULL DEFAULT ''"),
                 ("review_steps", "INTEGER NOT NULL DEFAULT 0"),
                 # memory: the stores frozen into this conversation's prompt and user turns
                 # since the last memory review; guest conversations are never searched
                 ("memory_snapshot", "TEXT"), ("memory_turns", "INTEGER NOT NULL DEFAULT 0"),
                 ("guest", "INTEGER NOT NULL DEFAULT 0")],
}

USAGE_FIELDS = ("calls", "prompt", "completion", "cache_hit", "cache_miss")

# Hermes's turn lease lives 300 s and a timer renews it every 60 s. Chelys has no timer
# thread and renews only between steps, and one step can include up to 3 provider retries,
# so the lease lives longer. A holder whose process is gone is reclaimed at once.
LEASE_TTL_S = 600.0


class SessionBusy(Exception):
    """Another Chelys process is running a turn in this conversation."""


def _pid_alive(pid: int) -> bool:
    if os.name != "posix":  # os.kill(pid, 0) is not a probe on Windows
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.fts = self._init_fts()

    def _migrate(self) -> None:
        with self.db:
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _init_fts(self) -> bool:
        """The session_search index: the text of user and assistant rows, keyed by message
        id. False if this SQLite has no FTS5. Built from existing rows the first time."""
        try:
            existed = self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone()
            with self.db:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING "
                                "fts5(text, tokenize = 'unicode61 remove_diacritics 2')")
        except sqlite3.OperationalError:
            return False
        if not existed:
            # Compaction re-inserts the rows it keeps, so old databases hold copies; index
            # only the first row with a given text in each conversation.
            seen: set[tuple[int, Any, str]] = set()
            rows = []
            for mid, sid, body in self.db.execute("SELECT id, session_id, body FROM messages ORDER BY id"):
                m = json.loads(body)
                text = search_text(m)
                key = (sid, m.get("role"), text)
                if text and key not in seen:
                    seen.add(key)
                    rows.append((mid, text))
            with self.db:
                self.db.executemany("INSERT INTO messages_fts (rowid, text) VALUES (?, ?)", rows)
        return True

    def close(self) -> None:
        self.db.close()

    def new_session(self) -> int:
        now = time.time()
        with self.db:
            cur = self.db.execute("INSERT INTO sessions (created, updated) VALUES (?, ?)", (now, now))
        return int(cur.lastrowid)

    def latest_session(self) -> int | None:
        row = self.db.execute("SELECT id FROM sessions ORDER BY updated DESC, id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None

    def session_exists(self, session_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None

    def list_sessions(self, limit: int = 20) -> list[tuple[int, float, str, int]]:
        return self.db.execute(
            """SELECT s.id, s.updated, s.title, COUNT(m.id)
               FROM sessions s LEFT JOIN messages m ON m.session_id = s.id AND m.active = 1
               GROUP BY s.id ORDER BY s.updated DESC, s.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def messages(self, session_id: int) -> list[dict[str, Any]]:
        """The live history (compacted-away rows excluded), oldest first."""
        rows = self.db.execute(
            "SELECT body FROM messages WHERE session_id = ? AND active = 1 ORDER BY id", (session_id,))
        return [json.loads(r[0]) for r in rows]

    # ---- writing history ----

    def last_turn(self, session_id: int) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(turn), 0) FROM messages WHERE session_id = ?", (session_id,)).fetchone()
        return int(row[0])

    def append(self, session_id: int, turn: int, rows: list[dict[str, Any]]) -> None:
        """Save rows of a turn now. Keys starting with "_" other than "_summary" are
        in-memory flags and are not stored."""
        if not rows:
            return
        now = time.time()
        with self.db:
            for m in rows:
                cur = self.db.execute("INSERT INTO messages (session_id, turn, body, ts) VALUES (?, ?, ?, ?)",
                                      (session_id, turn, json.dumps(_storable(m), ensure_ascii=False), now))
                text = search_text(m)
                if self.fts and text:
                    self.db.execute("INSERT INTO messages_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
            first_user = next((m.get("content") for m in rows if m.get("role") == "user"), "") or ""
            self.db.execute(
                "UPDATE sessions SET updated = ?, title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                (time.time(), first_user[:60], session_id),
            )

    def append_turn(self, session_id: int, turn_messages: list[dict[str, Any]]) -> None:
        """Save a whole turn at once as a new turn number."""
        self.append(session_id, self.last_turn(session_id) + 1, turn_messages)

    def replace_active(self, session_id: int, rows: list[dict[str, Any]]) -> None:
        """Compaction commit, in one transaction: archive every live row and insert `rows`
        as the new live history. Archived rows stay in the table (active = 0). The new rows
        copy rows that are already in the search index, so they aren't indexed again."""
        turn = self.last_turn(session_id)
        with self.db:
            self.db.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1", (session_id,))
            self.db.executemany(
                "INSERT INTO messages (session_id, turn, body, ts) VALUES (?, ?, ?, ?)",
                [(session_id, turn, json.dumps(_storable(m), ensure_ascii=False), time.time()) for m in rows],
            )

    def archived_count(self, session_id: int) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 0", (session_id,)).fetchone()
        return int(row[0])

    # ---- compaction cooldown ----

    def compact_retry_at(self, session_id: int) -> float:
        row = self.db.execute("SELECT compact_retry_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def set_compact_retry_at(self, session_id: int, when: float) -> None:
        with self.db:
            self.db.execute("UPDATE sessions SET compact_retry_at = ? WHERE id = ?", (when, session_id))

    # ---- turn lease ----

    def acquire_lease(self, session_id: int, holder: str, ttl: float = LEASE_TTL_S) -> None:
        """Take the session for one turn, or raise SessionBusy. The lease is free when it is
        unset, expired, already ours, or held by a process that no longer exists."""
        now = time.time()
        with self.db:
            cur = self.db.execute(
                """UPDATE sessions SET lease_holder = ?, lease_expires = ?
                   WHERE id = ? AND (lease_holder IS NULL OR lease_expires < ? OR lease_holder = ?)""",
                (holder, now + ttl, session_id, now, holder),
            )
        if cur.rowcount == 1:
            return
        row = self.db.execute("SELECT lease_holder, lease_expires FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise SessionBusy(f"no conversation #{session_id}")
        other, expires = row
        pid = _holder_pid(other)
        if pid is not None and pid != os.getpid() and not _pid_alive(pid):
            with self.db:
                cur = self.db.execute(
                    "UPDATE sessions SET lease_holder = ?, lease_expires = ? WHERE id = ? AND lease_holder = ?",
                    (holder, now + ttl, session_id, other),
                )
            if cur.rowcount == 1:
                return
        wait = max(0, int(expires - now))
        raise SessionBusy(f"conversation #{session_id} is busy with a turn in another Chelys process "
                          f"({other}); try again shortly, or start a new conversation (lease ends in {wait}s at most)")

    def renew_lease(self, session_id: int, holder: str, ttl: float = LEASE_TTL_S) -> None:
        with self.db:
            self.db.execute("UPDATE sessions SET lease_expires = ? WHERE id = ? AND lease_holder = ?",
                            (time.time() + ttl, session_id, holder))

    def release_lease(self, session_id: int, holder: str) -> None:
        with self.db:
            self.db.execute("UPDATE sessions SET lease_holder = NULL, lease_expires = 0 WHERE id = ? AND lease_holder = ?",
                            (session_id, holder))

    # ---- usage ----

    def log_usage(self, session_id: int, usage: dict[str, Any]) -> None:
        """Record one model call's token counts (DeepSeek's usage block)."""
        def n(key: str) -> int:
            v = usage.get(key)
            return int(v) if isinstance(v, (int, float)) else 0
        with self.db:
            self.db.execute(
                "INSERT INTO usage (session_id, ts, prompt, completion, cache_hit, cache_miss) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, time.time(), n("prompt_tokens"), n("completion_tokens"),
                 n("prompt_cache_hit_tokens"), n("prompt_cache_miss_tokens")),
            )

    def usage_totals(self, session_id: int | None = None) -> dict[str, int]:
        """Calls and token sums for one session, or for all of them."""
        sql = "SELECT COUNT(*), SUM(prompt), SUM(completion), SUM(cache_hit), SUM(cache_miss) FROM usage"
        if session_id is None:
            row = self.db.execute(sql).fetchone()
        else:
            row = self.db.execute(sql + " WHERE session_id = ?", (session_id,)).fetchone()
        return {k: int(v or 0) for k, v in zip(USAGE_FIELDS, row)}


    # ---- per-conversation skill and memory state ----

    SESSION_FIELDS = ("skills_index", "skills_noted", "review_steps", "memory_snapshot", "memory_turns", "guest")

    def session_field(self, session_id: int, column: str) -> Any:
        assert column in self.SESSION_FIELDS
        row = self.db.execute(f"SELECT {column} FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row[0] if row else None

    def set_session_field(self, session_id: int, column: str, value: Any) -> None:
        assert column in self.SESSION_FIELDS
        with self.db:
            self.db.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, session_id))

    # ---- session search ----

    def search(self, query: str, exclude: tuple[int, ...] = (), after: float | None = None,
               before: float | None = None, limit: int = 300) -> list[dict[str, Any]]:
        """Matching messages, best first (BM25), never from guest conversations. Raises
        sqlite3.OperationalError on bad FTS5 syntax."""
        sql = ("SELECT messages_fts.rowid, m.session_id, m.body, messages_fts.text, "
               "snippet(messages_fts, 0, '>>', '<<', '...', 24), COALESCE(m.ts, s.created), bm25(messages_fts) "
               "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
               "JOIN sessions s ON s.id = m.session_id WHERE messages_fts MATCH ? AND s.guest = 0")
        args: list[Any] = [query]
        if exclude:
            sql += f" AND m.session_id NOT IN ({', '.join('?' * len(exclude))})"
            args += list(exclude)
        if after is not None:
            sql += " AND COALESCE(m.ts, s.created) >= ?"
            args.append(after)
        if before is not None:
            sql += " AND COALESCE(m.ts, s.created) < ?"
            args.append(before)
        rows = self.db.execute(sql + " ORDER BY bm25(messages_fts) LIMIT ?", args + [limit]).fetchall()
        return [{"id": r[0], "session_id": r[1], "role": json.loads(r[2]).get("role"), "text": r[3],
                 "snippet": r[4], "ts": r[5], "rank": r[6]} for r in rows]

    def searchable_messages(self, session_id: int) -> list[dict[str, Any]]:
        """A conversation's indexed messages (user and assistant text), oldest first."""
        rows = self.db.execute(
            "SELECT m.id, m.body, messages_fts.text, COALESCE(m.ts, s.created) "
            "FROM messages m JOIN messages_fts ON messages_fts.rowid = m.id "
            "JOIN sessions s ON s.id = m.session_id WHERE m.session_id = ? ORDER BY m.id", (session_id,))
        return [{"id": r[0], "role": json.loads(r[1]).get("role"), "text": r[2], "ts": r[3]} for r in rows]

    def session_info(self, session_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT id, created, updated, title, guest FROM sessions WHERE id = ?",
                              (session_id,)).fetchone()
        return dict(zip(("id", "created", "updated", "title", "guest"), row)) if row else None

    def recent_sessions(self, exclude: tuple[int, ...] = (), limit: int = 10) -> list[dict[str, Any]]:
        sql = ("SELECT s.id, s.created, s.updated, s.title, COUNT(m.id) FROM sessions s "
               "LEFT JOIN messages m ON m.session_id = s.id AND m.active = 1 WHERE s.guest = 0")
        if exclude:
            sql += f" AND s.id NOT IN ({', '.join('?' * len(exclude))})"
        rows = self.db.execute(sql + " GROUP BY s.id ORDER BY s.updated DESC, s.id DESC LIMIT ?",
                               list(exclude) + [limit])
        return [dict(zip(("id", "created", "updated", "title", "count"), r)) for r in rows]

    # ---- skill revisions ----

    def add_revision(self, skill: str, file: str, actor: str, action: str,
                     before: str | None, after: str | None) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO skill_revisions (ts, skill, file, actor, action, before, after) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), skill, file, actor, action, before, after))
        return int(cur.lastrowid)

    def last_revision(self, skill: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id, ts, skill, file, actor, action, before, after FROM skill_revisions "
            "WHERE skill = ? AND undone = 0 ORDER BY id DESC LIMIT 1", (skill,)).fetchone()
        if row is None:
            return None
        return dict(zip(("id", "ts", "skill", "file", "actor", "action", "before", "after"), row))

    def mark_undone(self, rev_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE skill_revisions SET undone = 1 WHERE id = ?", (rev_id,))

    def revisions(self, skill: str | None = None, limit: int = 20) -> list[tuple]:
        sql = "SELECT id, ts, skill, file, actor, action, undone FROM skill_revisions"
        args: tuple = ()
        if skill:
            sql += " WHERE skill = ?"
            args = (skill,)
        return self.db.execute(sql + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()

    # ---- pending skill changes ----

    def add_pending(self, pid: str, session_id: int | None, origin: str, skill: str, gist: str,
                    payload: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("INSERT INTO skill_pending (id, ts, session_id, origin, skill, gist, payload) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (pid, time.time(), session_id, origin, skill, gist, json.dumps(payload)))

    def pending(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id, ts, session_id, origin, skill, gist, payload FROM skill_pending ORDER BY ts")
        return [dict(zip(("id", "ts", "session_id", "origin", "skill", "gist"), r[:6]), payload=json.loads(r[6]))
                for r in rows]

    def get_pending(self, pid: str) -> dict[str, Any] | None:
        return next((p for p in self.pending() if p["id"] == pid), None)

    def drop_pending(self, pid: str) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM skill_pending WHERE id = ?", (pid,))
        return cur.rowcount == 1

    # ---- ordinary tool approvals ----

    def grant_tool(self, action_key: str, session_id: int, tool: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tool_grants (action_key, session_id, tool) VALUES (?, ?, ?)",
                            (action_key, session_id, tool))

    def tool_granted(self, action_key: str, session_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM tool_grants WHERE (action_key = ? AND session_id = 0) "
            "OR (action_key = '*' AND session_id = ?)", (action_key, session_id)).fetchone() is not None

    def tool_grants(self) -> list[dict[str, Any]]:
        return [dict(zip(("id", "session_id", "tool", "action_key"), r)) for r in self.db.execute(
            "SELECT id, session_id, tool, action_key FROM tool_grants ORDER BY id")]

    def revoke_tool(self, grant_id: int | None = None) -> int:
        """None explicitly clears ordinary grants only; skill approvals are untouched."""
        with self.db:
            if grant_id is None:
                cur = self.db.execute("DELETE FROM tool_grants")
            else:
                cur = self.db.execute("DELETE FROM tool_grants WHERE id = ?", (grant_id,))
        return cur.rowcount

    # ---- standing skill approvals ----

    def grant(self, skill: str, session_id: int) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO skill_grants (skill, session_id) VALUES (?, ?)", (skill, session_id))

    def granted(self, skill: str, session_id: int | None) -> bool:
        """True if writes to this skill are pre-approved: always for this skill, or anything
        in this conversation."""
        row = self.db.execute(
            "SELECT 1 FROM skill_grants WHERE (skill = ? AND session_id = 0) OR (skill = '*' AND session_id = ?)",
            (skill, session_id if session_id is not None else -1)).fetchone()
        return row is not None

    def always_grants(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT skill FROM skill_grants WHERE session_id = 0 ORDER BY skill")]

    def revoke(self, skill: str) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM skill_grants WHERE skill = ? AND session_id = 0", (skill,))
        return cur.rowcount > 0

    # ---- skill usage (for the curator) ----

    def usage_bump(self, name: str, *, view: bool = False, use: bool = False, patch: bool = False,
                   created_by: str | None = None) -> None:
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO skill_usage (name, created_by, created) VALUES (?, ?, ?)",
                            (name, created_by, now if created_by else None))
            if created_by:
                self.db.execute("UPDATE skill_usage SET created_by = ?, created = ?, state = 'active', "
                                "archived_at = NULL WHERE name = ?", (created_by, now, name))
            sets = []
            if view:
                sets += ["view_count = view_count + 1", "last_viewed = :now"]
            if use:
                sets += ["use_count = use_count + 1", "last_used = :now"]
            if patch:
                sets += ["patch_count = patch_count + 1", "last_patched = :now"]
            if view or use or patch:
                sets.append("state = CASE WHEN state = 'stale' THEN 'active' ELSE state END")
                self.db.execute(f"UPDATE skill_usage SET {', '.join(sets)} WHERE name = :name",
                                {"now": now, "name": name})

    def usage_rows(self) -> list[dict[str, Any]]:
        cols = ("name", "created_by", "created", "use_count", "view_count", "patch_count", "last_used",
                "last_viewed", "last_patched", "state", "pinned", "archived_at")
        return [dict(zip(cols, r)) for r in self.db.execute(f"SELECT {', '.join(cols)} FROM skill_usage ORDER BY name")]

    def set_usage(self, name: str, **fields: Any) -> None:
        allowed = {"state", "pinned", "archived_at"}
        assert set(fields) <= allowed
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO skill_usage (name) VALUES (?)", (name,))
            for k, v in fields.items():
                self.db.execute(f"UPDATE skill_usage SET {k} = ? WHERE name = ?", (v, name))

    # ---- small key/value state ----

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def search_text(m: dict[str, Any]) -> str:
    """What session_search indexes for a row: the text of user and assistant messages. A
    user row's `content` is what was typed, without the time stamp or a carried summary."""
    if m.get("role") not in ("user", "assistant"):
        return ""
    c = m.get("content")
    return c.strip() if isinstance(c, str) else ""


def _storable(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if not k.startswith("_") or k == "_summary"}


def _holder_pid(holder: str | None) -> int | None:
    if not holder or not holder.startswith("pid="):
        return None
    try:
        return int(holder[4:].split(":", 1)[0])
    except ValueError:
        return None


def row_chars(m: dict[str, Any]) -> int:
    """Size of one row as sent. Keys starting with "_" never reach the model; counting
    `_summary` measured every summary twice (it is also in the row's api_content)."""
    return len(json.dumps({k: v for k, v in m.items() if not k.startswith("_")}, ensure_ascii=False))


def history_chars(history: list[dict[str, Any]]) -> int:
    """Size of history as recent_window measures it."""
    return sum(row_chars(m) for m in history)


def recent_window(history: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """The slice of history sent to the model, trimmed in blocks so its start stays put.

    DeepSeek caches the longest prefix it has seen before. Dropping one old turn per new
    turn would move the window's start, and so break that prefix, on every message.
    Instead the possible start points form a fixed chain: from one start, the next is the
    first user message at least a quarter of max_chars further on. The chain depends only
    on older history, so it doesn't shift as the conversation grows. The window starts at
    the first point in the chain from which everything fits, so it jumps forward a block
    at a time and stays still (cacheable) in between.

    Compaction normally keeps history under max_chars; this trim is the backstop for a
    single turn that outgrows it, or a summary call that failed.

    The slice always starts at a user message, so it never begins with a tool result or an
    assistant tool call cut off from its pair. The newest turn is always kept.
    """
    starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
    if not starts:
        return []
    suffix = [0] * (len(history) + 1)
    for i in range(len(history) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + row_chars(history[i])
    block = max(1, max_chars // 4)
    pos = starts[0]
    while suffix[pos] > max_chars and pos != starts[-1]:
        pos = next((q for q in starts if q > pos and suffix[pos] - suffix[q] >= block), starts[-1])
    return history[pos:]
