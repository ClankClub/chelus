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
"""

# Columns added after v0.1.0; existing databases get them on open.
MIGRATIONS = {
    "messages": [("active", "INTEGER NOT NULL DEFAULT 1")],
    "sessions": [("lease_holder", "TEXT"), ("lease_expires", "REAL NOT NULL DEFAULT 0"),
                 ("compact_retry_at", "REAL NOT NULL DEFAULT 0")],
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

    def _migrate(self) -> None:
        with self.db:
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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
        with self.db:
            self.db.executemany(
                "INSERT INTO messages (session_id, turn, body) VALUES (?, ?, ?)",
                [(session_id, turn, json.dumps(_storable(m), ensure_ascii=False)) for m in rows],
            )
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
        as the new live history. Archived rows stay in the table (active = 0)."""
        turn = self.last_turn(session_id)
        with self.db:
            self.db.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1", (session_id,))
            self.db.executemany(
                "INSERT INTO messages (session_id, turn, body) VALUES (?, ?, ?)",
                [(session_id, turn, json.dumps(_storable(m), ensure_ascii=False)) for m in rows],
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


def _storable(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if not k.startswith("_") or k == "_summary"}


def _holder_pid(holder: str | None) -> int | None:
    if not holder or not holder.startswith("pid="):
        return None
    try:
        return int(holder[4:].split(":", 1)[0])
    except ValueError:
        return None


def history_chars(history: list[dict[str, Any]]) -> int:
    """Size of history as recent_window measures it."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in history)


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
        suffix[i] = suffix[i + 1] + len(json.dumps(history[i], ensure_ascii=False))
    block = max(1, max_chars // 4)
    pos = starts[0]
    while suffix[pos] > max_chars and pos != starts[-1]:
        pos = next((q for q in starts if q > pos and suffix[pos] - suffix[q] >= block), starts[-1])
    return history[pos:]
