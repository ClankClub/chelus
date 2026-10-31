"""Long tool output is saved to a file instead of being thrown away (Hermes's
tool_result_storage and web_tools_truncate, docs/hermes/tools.md item 3).

The model sees the head and tail of the output, cut on line boundaries, plus the path of
the full text and the read_file call that pages the part it didn't see. Files live in
Chelus's cache folder (0700, files 0600) and are deleted after a day. Shell output is
saved with credential-looking strings masked, line for line, so the line numbers in the
footer still match the file.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

from . import compaction

KEEP_S = 24 * 3600
MAX_SAVED_CHARS = 2_000_000
HEAD_SHARE = 0.75


def spill_dir(s) -> Path:
    return Path(s.cache_dir) / "spill"


def shown_path(path: Path) -> str:
    """The path as the model should type it: ~/... when it is under home."""
    home = str(Path.home())
    p = str(path)
    if p.startswith(home + os.sep):
        return "~/" + p[len(home) + 1:].replace(os.sep, "/")
    return p


def prune(folder: Path, now: float | None = None) -> None:
    now = time.time() if now is None else now
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and now - entry.stat().st_mtime > KEEP_S:
                entry.unlink()
        except OSError:
            pass


def save(s, label: str, text: str, redact: bool = True) -> Path | None:
    """Write text to a new spill file; its path, or None if it couldn't be written."""
    folder = spill_dir(s)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
    except OSError:
        return None
    prune(folder)
    if redact:
        text = compaction.redact(text, keep_lines=True)
    if len(text) > MAX_SAVED_CHARS:
        text = text[:MAX_SAVED_CHARS] + f"\n[saved copy stopped after {MAX_SAVED_CHARS} characters]\n"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:30] or "output"
    path = folder / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.txt"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError:
        return None
    return path


def _window(text: str, limit: int) -> tuple[str, str]:
    """Head and tail of text within about limit characters, cut at line ends where possible."""
    head_budget = int(limit * HEAD_SHARE)
    tail_budget = limit - head_budget
    head = text[:head_budget]
    nl = head.rfind("\n")
    if nl > head_budget // 2:
        head = head[: nl + 1]
    tail = text[-tail_budget:] if tail_budget > 0 else ""
    nl = tail.find("\n")
    if 0 <= nl < tail_budget // 2:
        tail = tail[nl + 1:]
    return head, tail


def fit(s, label: str, text: str, limit: int, redact: bool = True, what: str = "output") -> str:
    """text if it fits in limit characters; otherwise its head and tail, with the full text
    saved and a footer that says how to read the middle. Falls back to a plain cut when the
    file can't be written."""
    if len(text) <= limit:
        return text
    head, tail = _window(text, limit)
    path = save(s, label, text, redact)
    hidden = len(text) - len(head) - len(tail)
    if path is None:
        return (text[:limit] + f"\n... [cut off: {len(text) - limit} more characters; "
                "the full text could not be saved]")
    first = head.count("\n") + 1
    last = text.count("\n") + 1 - tail.count("\n")  # the line the tail starts on
    where = shown_path(path)
    footer = (f"\n... [{hidden} characters of {what} not shown here (lines {first} to about {max(first, last - 1)}). "
              f"The full {what} ({len(text)} characters) is saved: read_file path={where} offset={first}]\n")
    return head + footer + tail
