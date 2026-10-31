"""search_files: content search by regex, or file-name search by glob (Hermes's search tool,
docs/hermes/tools.md item 4).

Content search uses ripgrep when it is installed and a Python walk otherwise. Neither ever
reports the off-limits folders (Chelus's keys, settings, history and ~/.ssh): the search
root is checked, ripgrep is told to skip them, and every result path is checked again
before it is shown. Hidden folders, .git and node_modules are skipped unless the search
starts inside one. Guests are kept to the work folder like the other file tools.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import tools
from .tools import ToolError

DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MAX_CONTEXT = 3
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 20000          # files the Python walk looks at before giving up
DEADLINE_S = 20
LINE_CHARS = 300
SKIP_DIRS = {".git", "node_modules", "__pycache__"}


def _walk(root: Path, guest: bool, deadline: float):
    """Files under root, skipping hidden folders and heavy ones. Yields (path, truncated)."""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") and guest:
                continue
            seen += 1
            if seen > MAX_FILES or time.monotonic() > deadline:
                yield None
                return
            yield Path(dirpath) / name


def _allowed(s, path: Path, ctx: Any) -> bool:
    try:
        tools._resolve(s, str(path), ctx)
    except ToolError:
        return False
    return True


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        return str(path)


def _clip(line: str) -> str:
    line = line.rstrip("\r\n")
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + " [...]"


def _find_names(s, root: Path, pattern: str, limit: int, ctx: Any) -> tuple[list[str], bool]:
    out, deadline = [], time.monotonic() + DEADLINE_S
    guest = getattr(ctx, "guest", False)
    for p in _walk(root, guest, deadline):
        if p is None:
            return out, True
        if fnmatch.fnmatch(p.name, pattern) or fnmatch.fnmatch(_rel(p, root), pattern):
            if _allowed(s, p, ctx):
                out.append(_rel(p, root))
                if len(out) >= limit:
                    return out, True
    return out, False


def _python_grep(s, root: Path, rx: re.Pattern, glob: str | None, limit: int, context: int,
                 files_only: bool, ctx: Any) -> tuple[list[str], bool]:
    out, hits, deadline = [], 0, time.monotonic() + DEADLINE_S
    guest = getattr(ctx, "guest", False)
    paths = [root] if root.is_file() else _walk(root, guest, deadline)
    for p in paths:
        if p is None:
            return out, True
        if glob and not fnmatch.fnmatch(p.name, glob):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES or not _allowed(s, p, ctx):
                continue
            with p.open("rb") as f:
                if b"\0" in f.read(8192):
                    continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = _rel(p, root) if root.is_dir() else p.name
        matched = [i for i, l in enumerate(lines) if rx.search(l)]
        if not matched:
            continue
        if files_only:
            out.append(rel)
            hits += 1
            if hits >= limit:
                return out, True
            continue
        shown: set[int] = set()
        for i in matched:
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                if j in shown:
                    continue
                shown.add(j)
                sep = ":" if j == i or j in matched else "-"
                out.append(f"{rel}{sep}{j + 1}{sep} {_clip(lines[j])}")
            hits += 1
            if hits >= limit:
                return out, True
    return out, False


def _rg(s, root: Path, pattern: str, glob: str | None, limit: int, context: int,
        files_only: bool, ctx: Any) -> tuple[list[str], bool] | None:
    """ripgrep's answer, or None when it can't be used (not installed, failed, bad regex)."""
    exe = shutil.which("rg")
    if not exe:
        return None
    argv = [exe, "--json", "--max-filesize", str(MAX_FILE_BYTES), "--no-config", "-e", pattern]
    if context:
        argv += ["-C", str(context)]
    if glob:
        argv += ["--glob", glob]
    for d in SKIP_DIRS:
        argv += ["--glob", f"!{d}"]
    if root.is_dir():
        for blocked in tools.off_limits(s):
            real = os.path.realpath(blocked)
            if tools._inside(real, str(root)) and real != str(root):
                argv += ["--glob", f"!/{_rel(Path(real), root)}"]
    argv.append(str(root))
    try:
        code, stdout, _err, exceeded = tools.run_bounded(argv, str(root if root.is_dir() else root.parent),
                                                         DEADLINE_S, limit=4 * 1024 * 1024)
    except subprocess.TimeoutExpired:
        return None
    if code not in (0, 1) and not exceeded:
        return None
    out, hits, last_file = [], 0, None
    base = root if root.is_dir() else root.parent
    for raw in stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind not in ("match", "context"):
            continue
        data = ev.get("data", {})
        text_path = (data.get("path") or {}).get("text")
        if not text_path:
            continue
        p = Path(text_path)
        if not _allowed(s, p, ctx):
            continue
        rel = _rel(p, base)
        if files_only:
            if kind == "match" and rel != last_file:
                out.append(rel)
                last_file = rel
                hits += 1
        else:
            line = (data.get("lines") or {}).get("text") or ""
            sep = ":" if kind == "match" else "-"
            out.append(f"{rel}{sep}{data.get('line_number')}{sep} {_clip(line)}")
            if kind == "match":
                hits += 1
        if hits >= limit:
            return out, True
    return out, exceeded


def _search_files(s, args: dict[str, Any], ctx: Any = None) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern must be a non-empty string")
    mode = args.get("mode") or "content"
    if mode not in ("content", "names"):
        raise ToolError("mode must be content or names")
    root = tools._resolve(s, args.get("path") or ".", ctx)
    if not root.exists():
        raise ToolError(f"no such file or directory: {root}")
    limit = tools._int_arg(args, "limit", 1, MAX_LIMIT) or DEFAULT_LIMIT
    context = tools._int_arg(args, "context", 0, MAX_CONTEXT) or 0
    files_only = args.get("files_only") in (True, "true")
    glob = args.get("glob") if isinstance(args.get("glob"), str) and args.get("glob") else None
    if mode == "names":
        if not root.is_dir():
            raise ToolError(f"not a directory: {root}")
        found, more = _find_names(s, root, pattern, limit, ctx)
        what = "files"
    else:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"bad regular expression: {e}") from None
        result = _rg(s, root, pattern, glob, limit, context, files_only, ctx)
        if result is None:
            result = _python_grep(s, root, rx, glob, limit, context, files_only, ctx)
        found, more = result
        what = "files" if files_only else "matching lines"
    if not found:
        return f"No {what} found for {pattern!r} under {root}."
    head = f"{root}:"
    tail = (f"\n[stopped at {limit} {'files' if what == 'files' else 'matches'}, or the search limit; "
            "narrow the path, glob or pattern for more]") if more else ""
    return head + "\n" + "\n".join(found) + tail


tools.register(tools.Tool(
    "search_files",
    "Search files on the phone. Use this instead of grep, rg or find in run_shell. mode=content "
    "(default) finds lines matching a regex; mode=names finds files whose name matches a glob like "
    "'*.py'. Hidden folders, .git and node_modules are skipped unless path is inside one.",
    tools.obj({"pattern": {"type": "string", "description": "Regex for content, glob for names."},
               "path": {"type": "string", "description": "File or folder to search; default the working folder."},
               "mode": {"type": "string", "enum": ["content", "names"]},
               "glob": {"type": "string", "description": "Content mode: only files whose name matches, e.g. '*.md'."},
               "context": {"type": "integer", "description": f"Lines around each match (0-{MAX_CONTEXT})."},
               "files_only": {"type": "boolean", "description": "Content mode: list matching files, not lines."},
               "limit": {"type": "integer", "description": f"Most results (default {DEFAULT_LIMIT}, at most {MAX_LIMIT})."}},
              ["pattern"]),
    _search_files,
))
