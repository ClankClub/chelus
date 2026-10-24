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
"""


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


def recent_window(history: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """The newest slice of history that fits the character budget.

    The slice always starts at a user message, so it never begins with a tool result
    or an assistant tool call cut off from its pair. The newest turn is always kept.
    """
    starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
    if not starts:
        return []
    suffix = [0] * (len(history) + 1)
    for i in range(len(history) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + len(json.dumps(history[i], ensure_ascii=False))
    chosen = starts[-1]
    for i in reversed(starts[:-1]):
        if suffix[i] > max_chars:
            break
        chosen = i
    return history[chosen:]
