"""Conversation history in SQLite. A turn is saved all at once, so a cancelled turn
never leaves half a tool exchange in the history."""

from __future__ import annotations

import json
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

USAGE_FIELDS = ("calls", "prompt", "completion", "cache_hit", "cache_miss")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

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
               FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
               GROUP BY s.id ORDER BY s.updated DESC, s.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def messages(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT body FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
        return [json.loads(r[0]) for r in rows]

    def append_turn(self, session_id: int, turn_messages: list[dict[str, Any]]) -> None:
        """Save one completed turn (user message, any tool exchanges, final reply) atomically."""
        if not turn_messages:
            return
        with self.db:
            row = self.db.execute("SELECT COALESCE(MAX(turn), 0) FROM messages WHERE session_id = ?", (session_id,)).fetchone()
            turn = int(row[0]) + 1
            self.db.executemany(
                "INSERT INTO messages (session_id, turn, body) VALUES (?, ?, ?)",
                [(session_id, turn, json.dumps(m, ensure_ascii=False)) for m in turn_messages],
            )
            first_user = next((m.get("content") for m in turn_messages if m.get("role") == "user"), "") or ""
            self.db.execute(
                "UPDATE sessions SET updated = ?, title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                (time.time(), first_user[:60], session_id),
            )

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


def recent_window(history: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """The slice of history sent to the model, trimmed in blocks so its start stays put.

    DeepSeek caches the longest prefix it has seen before. Dropping one old turn per new
    turn would move the window's start, and so break that prefix, on every message.
    Instead the possible start points form a fixed chain: from one start, the next is the
    first user message at least a quarter of max_chars further on. The chain depends only
    on older history, so it doesn't shift as the conversation grows. The window starts at
    the first point in the chain from which everything fits, so it jumps forward a block
    at a time and stays still (cacheable) in between.

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
