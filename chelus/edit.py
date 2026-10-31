"""edit_file: find-and-replace edits with a diff on the approval card (Hermes's patch tool,
docs/hermes/tools.md item 5).

The match is worked out before the owner is asked, so a miss costs no approval tap, and
the card shows the unified diff. When the exact text isn't in the file, six normalizing
strategies are tried in order: trailing whitespace, inner whitespace runs, indentation,
escaped newlines, blank lines at the ends, and typographic quotes and dashes. Hermes's two
similarity strategies are left out: their match can land a line off, which is hard to spot
on a phone. The match must be unique unless replace_all is set. Line endings (CRLF) and a
UTF-8 BOM are kept. An edit that is already in the file is a no-op success.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import tools
from .tools import ToolError

MAX_BYTES = 2 * 1024 * 1024
CARD_DIFF_CHARS = 2500
MISSES_BEFORE_HINT = 3

_UNICODE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                          "—": "-", " ": " ", "−": "-"})


@dataclass
class Plan:
    path: Path
    old_text: str      # the file as it is, line endings as on disk
    new_text: str      # the file after the edit
    diff: str
    count: int
    strategy: str
    note: str = ""


# ---- matching ----

def _line_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each line's content, end excluding the newline."""
    spans, pos = [], 0
    for line in text.split("\n"):
        spans.append((pos, pos + len(line)))
        pos += len(line) + 1
    return spans


def _collapse_inner(line: str) -> str:
    indent = line[:len(line) - len(line.lstrip())]
    return indent + " ".join(line.split())


LINE_STRATEGIES: list[tuple[str, Callable[[str], str]]] = [
    ("trailing whitespace ignored", str.rstrip),
    ("whitespace runs ignored", _collapse_inner),
    ("indentation ignored", lambda l: " ".join(l.split())),
    ("quotes and dashes normalized", lambda l: " ".join(l.translate(_UNICODE).split())),
]


def _by_lines(text: str, old: str, norm: Callable[[str], str]) -> list[tuple[int, int]]:
    """Spans of text whose lines equal old's lines after norm. Spans cover whole lines."""
    want = [norm(l) for l in old.split("\n")]
    if not any(w.strip() for w in want):
        return []
    lines = text.split("\n")
    spans = _line_spans(text)
    got = [norm(l) for l in lines]
    out, i, n = [], 0, len(want)
    while i + n <= len(lines):
        if got[i:i + n] == want:
            out.append((spans[i][0], spans[i + n - 1][1]))
            i += n
        else:
            i += 1
    return out


def _exact(text: str, old: str) -> list[tuple[int, int]]:
    out, start = [], 0
    while old:
        at = text.find(old, start)
        if at < 0:
            break
        out.append((at, at + len(old)))
        start = at + len(old)
    return out


def find(text: str, old: str, new: str) -> tuple[list[tuple[int, int]], str, str, str]:
    """(spans, strategy, old used, new used). Both strings already have \\n line endings."""
    hits = _exact(text, old)
    if hits:
        return hits, "exact", old, new
    candidates = [(old, new, "")]
    if "\n" not in old and ("\\n" in old or "\\t" in old):
        def unescape(x: str) -> str:
            return x.replace("\\n", "\n").replace("\\t", "\t")
        candidates.append((unescape(old), unescape(new), "escaped newlines read as newlines"))
    trimmed = old.strip("\n")
    if trimmed != old and trimmed:
        candidates.append((trimmed, new.strip("\n"), "blank lines at the ends ignored"))
    for o, n_, label in candidates[1:]:
        hits = _exact(text, o)
        if hits:
            return hits, label, o, n_
    for o, n_, label in candidates:
        for name, norm in LINE_STRATEGIES:
            hits = _by_lines(text, o, norm)
            if hits:
                return hits, ", ".join(x for x in (label, name) if x), o, n_
    return [], "", old, new


def _reindent(new: str, matched_first_line: str, old_first_line: str) -> str:
    """When indentation was ignored, give new the indentation the file has."""
    have = matched_first_line[:len(matched_first_line) - len(matched_first_line.lstrip())]
    gave = old_first_line[:len(old_first_line) - len(old_first_line.lstrip())]
    if have == gave or not have.startswith(gave):
        return new
    extra = have[len(gave):]
    return "\n".join(extra + l if l.strip() else l for l in new.split("\n"))


def _closest(text: str, old: str) -> str:
    first = next((l for l in old.split("\n") if l.strip()), "")
    if not first:
        return ""
    lines = text.split("\n")
    stripped = [l.strip() for l in lines]
    best = difflib.get_close_matches(first.strip(), stripped, n=3, cutoff=0.5)
    shown = []
    for b in best:
        i = stripped.index(b)
        shown.append(f"  line {i + 1}: {lines[i].strip()[:160]}")
    return ("Closest lines to the first line of old_string:\n" + "\n".join(shown)) if shown else ""


# ---- planning an edit ----

def _read(path: Path) -> tuple[str, bool, bool]:
    """(text with \\n endings, had BOM, used CRLF)."""
    if not path.is_file():
        raise ToolError(f"not a file: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ToolError(f"{path} is {len(raw)} bytes; edit_file handles files up to {MAX_BYTES}")
    if b"\0" in raw[:8192]:
        raise ToolError(f"{path} looks like a binary file")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw[3 if bom else 0:].decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} isn't UTF-8 text; edit_file won't risk rewriting it") from None
    crlf = "\r\n" in text
    return (text.replace("\r\n", "\n") if crlf else text), bom, crlf


def _diff(path: Path, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        f"a/{path.name}", f"b/{path.name}", n=2))


def plan(s, args: dict[str, Any], ctx: Any = None) -> Plan:
    path = tools._resolve(s, args.get("path"), ctx)
    old, new = args.get("old_string"), args.get("new_string")
    if not isinstance(old, str) or not old:
        raise ToolError("old_string must be the exact text to replace, not empty. To create a file "
                        "use write_file.")
    if not isinstance(new, str):
        raise ToolError("new_string must be a string (empty to delete the text)")
    replace_all = args.get("replace_all") in (True, "true")
    if old == new:
        raise ToolError("old_string and new_string are the same; nothing to change")
    text, bom, crlf = _read(path)
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    hits, strategy, old_used, new_used = find(text, old, new)
    if not hits:
        if new.strip() and (new in text or _by_lines(text, new, LINE_STRATEGIES[0][1])):
            raise _AlreadyDone(path)
        _miss(ctx, path)
        hint = _closest(text, old)
        misses = getattr(ctx, "_edit_misses", {}).get(str(path), 0)
        more = ("\nThat's several misses on this file: read it again with read_file before the "
                "next edit." if misses >= MISSES_BEFORE_HINT else "")
        raise ToolError(f"old_string was not found in {path}." + (f"\n{hint}" if hint else "") + more)
    if len(hits) > 1 and not replace_all:
        where = ", ".join(str(text.count("\n", 0, a) + 1) for a, _ in hits[:10])
        raise ToolError(f"old_string matches {len(hits)} places in {path} (lines {where}). Add surrounding "
                        "lines to make it unique, or set replace_all=true.")
    pieces, last = [], 0
    for a, b in hits:
        repl = new_used
        if "indentation" in strategy or "quotes" in strategy:
            first_line = text[a:b].split("\n", 1)[0]
            repl = _reindent(new_used, first_line, old_used.split("\n", 1)[0])
        pieces += [text[last:a], repl]
        last = b
    after = "".join(pieces) + text[last:]
    diff = _diff(path, text, after)
    note = "" if strategy == "exact" else f"Matched with {strategy}; check the diff."
    disk_before = _encode(text, bom, crlf)
    return Plan(path, disk_before, _encode(after, bom, crlf), diff, len(hits), strategy, note)


def _encode(text: str, bom: bool, crlf: bool) -> str:
    return ("﻿" if bom else "") + (text.replace("\n", "\r\n") if crlf else text)


class _AlreadyDone(ToolError):
    def __init__(self, path: Path):
        super().__init__(f"no change: new_string is already in {path}, so this edit looks already applied")
        self.path = path


def _miss(ctx: Any, path: Path) -> None:
    if ctx is None:
        return
    misses = getattr(ctx, "_edit_misses", None)
    if misses is None:
        misses = {}
        try:
            setattr(ctx, "_edit_misses", misses)
        except AttributeError:
            return
    misses[str(path)] = misses.get(str(path), 0) + 1


# ---- the tool ----

def prepare(s, args: dict[str, Any], ctx: Any = None) -> str | None:
    """Before approval: work out the edit. Returns a final answer when there is nothing to
    approve (already applied); raises ToolError on a miss."""
    try:
        p = plan(s, args, ctx)
    except _AlreadyDone as e:
        return str(e)
    args["path"] = str(p.path)
    args["_diff"] = p.diff
    args["_note"] = p.note
    return None


def _edit_file(s, args: dict[str, Any], ctx: Any = None) -> str:
    try:
        p = plan(s, args, ctx)
    except _AlreadyDone as e:
        return str(e)
    if "_diff" in args and p.diff != args["_diff"]:
        raise ToolError(f"{p.path} changed while waiting for approval; nothing was written. Try again.")
    with open(p.path, "w", encoding="utf-8", newline="") as f:
        f.write(p.new_text)
    if ctx is not None and getattr(ctx, "_edit_misses", None):
        ctx._edit_misses.pop(str(p.path), None)
    head = f"edited {p.path}: {p.count} replacement{'s' if p.count > 1 else ''}"
    return head + (f" ({p.note})" if p.note else "") + "\n" + p.diff


def card_diff(diff: str) -> str:
    if len(diff) <= CARD_DIFF_CHARS:
        return diff
    cut = diff[:CARD_DIFF_CHARS].rsplit("\n", 1)[0]
    rest = diff[len(cut):].count("\n")
    return cut + f"\n... ({rest} more diff lines)"


def write_hint(path: Path, new_content: str) -> str:
    """For write_file: a nudge when a long file was re-sent mostly unchanged."""
    try:
        if not path.is_file() or path.stat().st_size < 3000:
            return ""
        before = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    after = new_content.splitlines()
    if not before or not after:
        return ""
    kept = len(set(before) & set(after)) / max(len(set(before)), 1)
    if kept >= 0.9:
        return ("\nTip: most of this file was unchanged. For small changes, edit_file sends only the "
                "changed text and shows the owner a diff.")
    return ""


tools.register(tools.Tool(
    "edit_file",
    "Change part of a text file by replacing old_string with new_string. Use this instead of "
    "write_file or sed for edits to existing files. old_string must match one place (copy it from "
    "read_file, with a line or two of context); set replace_all to change every match. Small "
    "whitespace and quote differences are tolerated. Asks the owner first, showing the diff.",
    tools.obj({"path": {"type": "string"},
               "old_string": {"type": "string", "description": "The text to replace, as in the file."},
               "new_string": {"type": "string", "description": "The replacement (empty deletes it)."},
               "replace_all": {"type": "boolean", "description": "Replace every match. Default false."},
               "purpose": tools.PURPOSE},
              ["path", "old_string", "new_string", "purpose"]),
    _edit_file,
    risky=True,
    prepare=prepare,
))
