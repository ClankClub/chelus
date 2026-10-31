"""Phone-local tools. Each one has an OpenAI-style schema, a handler and a risk flag.

Risky tools (writing files, running shell commands) go through the agent's permission
callback before they run. Arbitrary shell access is not a sandbox; the gate is the safety.

File tools never touch Chelus's own config and data directories (API key, bot token,
history, skills, memory) or ~/.ssh, for anyone. Skills and memory are read and written
through their own tools, which have their own checks. Guests are further limited to non-hidden paths under
the working directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import approval, jobs
from .config import Settings


class ToolError(Exception):
    """A tool failure the model should see and can react to."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]    # (settings, args, ctx=None) -> output
    risky: bool = False
    group: str = "core"            # "skills", "memory": owner only, never offered to guests
    max_output: int | None = None  # output cap, if not tool_output_chars
    spill: bool = True             # output past the cap is saved for paging, not just cut
    # Runs before approval: may fill in args for the card, raise ToolError (no prompt), or
    # return a final answer when there is nothing to approve.
    prepare: Callable[..., str | None] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


def off_limits(s: Settings) -> list[Path]:
    return [s.config_dir, s.data_dir, Path.home() / ".ssh"]


def _resolve(s: Settings, path: Any, ctx: Any = None) -> Path:
    """The real path a file tool may touch, or ToolError. Symlinks are followed before the
    check, so a link can't lead into a blocked directory."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string")
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path(s.workdir) / p
    real = os.path.realpath(p)
    for root in off_limits(s):
        if _inside(real, os.path.realpath(root)):
            raise ToolError(f"{p} is off limits: that directory holds Chelus's keys, settings, history "
                            "or skills. Use skills_list and skill_view for skills.")
    if getattr(ctx, "guest", False):
        work = os.path.realpath(s.workdir)
        rel = os.path.relpath(real, work) if _inside(real, work) else None
        if rel is None or any(part.startswith(".") and part != "." for part in Path(rel).parts):
            raise ToolError(f"{p} is outside what guests can see (non-hidden files under {s.workdir})")
    return Path(real)


def _list_dir(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    p = _resolve(s, args.get("path") or ".", ctx)
    if not p.is_dir():
        raise ToolError(f"not a directory: {p}")
    rows = []
    for entry in sorted(p.iterdir(), key=lambda e: e.name):
        if getattr(ctx, "guest", False) and entry.name.startswith("."):
            continue
        try:
            _resolve(s, str(entry), ctx)
        except ToolError:
            continue  # do not stat or disclose protected children, even through links
        try:
            if entry.is_dir():
                rows.append(f"{entry.name}/")
            else:
                rows.append(f"{entry.name}  {entry.stat().st_size} bytes")
        except OSError:
            rows.append(f"{entry.name}  (unreadable)")
        if len(rows) >= 200:
            break
    return f"{p}\n" + ("\n".join(rows) if rows else "(empty)")


READ_MAX_LINES = 2000
COUNT_LINES_UP_TO = 20 * 1024 * 1024  # past this size the total line count is not worked out


def _int_arg(args: dict[str, Any], key: str, low: int, high: int | None = None) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ToolError(f"{key} must be a whole number")
    value = int(value)
    if value < low or (high is not None and value > high):
        raise ToolError(f"{key} must be between {low} and {high}" if high else f"{key} must be at least {low}")
    return value


def read_budget(s: Settings) -> int:
    """Characters of file text read_file returns per call, so its result never needs cutting."""
    return max(1000, s.tool_output_chars - 300)


def _read_file(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    """A file, or a window of its lines. A file that fits comes back as is; otherwise as
    much as fits, with a footer naming the offset to read on from."""
    p = _resolve(s, args.get("path"), ctx)
    if not p.is_file():
        raise ToolError(f"not a file: {p}")
    offset = _int_arg(args, "offset", 1) or 1
    limit = _int_arg(args, "limit", 1, READ_MAX_LINES) or READ_MAX_LINES
    budget = read_budget(s)
    size = p.stat().st_size
    with p.open("rb") as f:
        if b"\0" in f.read(8192):
            raise ToolError(f"{p} looks like a binary file ({size} bytes), not text")
    count_all = size <= COUNT_LINES_UP_TO
    lines: list[str] = []
    chars = total = end = 0
    cut_line = more = False
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for total, line in enumerate(f, 1):
            if total < offset:
                continue
            if more:
                if not count_all:
                    total = 0
                    break
                continue
            if len(lines) >= limit or chars + len(line) > budget:
                if not lines:  # one line longer than the whole budget
                    lines.append(line[:budget] + "\n")
                    cut_line, end = True, total
                more = True
                continue
            lines.append(line)
            chars += len(line)
            end = total
    if offset > 1 and not lines:
        raise ToolError(f"offset {offset} is past the end of {p} ({total} lines)")
    text = "".join(lines)
    if offset == 1 and not more and not cut_line:
        return text
    of = f"of {total}" if total else "of a file too big to count"
    note = f"[{p}: lines {offset}-{end} {of}"
    if cut_line:
        note += f"; line {end} is longer than {budget} characters and was cut"
    if more:
        note += f". Read on with read_file offset={end + 1}"
    return text + ("" if text.endswith("\n") or not text else "\n") + note + "]"


def _write_file(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    p = _resolve(s, args.get("path"), ctx)
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if p.is_dir():
        raise ToolError(f"is a directory: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    from .edit import write_hint
    hint = write_hint(p, content)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content.encode('utf-8'))} bytes to {p}" + hint


def run_bounded(argv: list[str], cwd: str, timeout: float, limit: int = 65536,
                env: dict[str, str] | None = None) -> tuple[int | None, str, str, bool]:
    """Run argv in its own process group with no stdin. Returns (exit code, stdout, stderr,
    exceeded). Past `limit` bytes of output the group is stopped and exceeded is True. Raises
    subprocess.TimeoutExpired after killing the group when it runs past `timeout` seconds."""
    import selectors
    import time

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    total, exceeded = 0, False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in buffers:
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(remaining):
                    chunk = key.fileobj.read1(8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    keep = min(len(chunk), limit - total)
                    buffers[key.fileobj].extend(chunk[:keep])
                    total += keep
                    if keep < len(chunk):
                        exceeded = True
                        _kill_group(proc)
                        break
                if exceeded:
                    break
            if not exceeded:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        for pipe in buffers:
            pipe.close()
    return (proc.returncode, buffers[proc.stdout].decode("utf-8", errors="replace"),
            buffers[proc.stderr].decode("utf-8", errors="replace"), exceeded)


def shell_mode(s: Settings, args: dict[str, Any]) -> tuple[bool, float]:
    """(background, timeout) for a run_shell call. A timeout past the foreground limit
    sends the command to the background instead of being refused (Hermes does the same)."""
    asked = _int_arg(args, "timeout", 1)
    flag = args.get("background")
    if flag not in (None, True, False, "true", "false"):
        raise ToolError("background must be true or false")
    background = flag in (True, "true") or (asked is not None and asked > s.shell_timeout)
    if background:
        return True, float(jobs.LIMIT_S)
    return False, float(min(asked or s.shell_timeout, s.shell_timeout))


def _run_shell(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")
    background, timeout = shell_mode(s, args)
    if background:
        if getattr(ctx, "guest", False):
            raise ToolError("background runs are for the owner only")
        purpose = args.get("purpose") if isinstance(args.get("purpose"), str) else ""
        try:
            job = jobs.start(s, command, purpose)
        except jobs.JobError as e:
            raise ToolError(str(e)) from None
        return (f"started background job {job['id']} (stopped after {jobs.LIMIT_S // 60} minutes if still "
                "running). The owner gets a message when it ends, and so do you on their next turn. Don't "
                f"wait or poll in a loop: check it with shell_jobs action=log id={job['id']} only if needed now.")
    shell = shutil.which("bash") or shutil.which("sh") or "/system/bin/sh"
    limit = 65536
    # Own process group, so a timeout or Ctrl-C kills the command and anything it spawned.
    try:
        code, out, err, exceeded = run_bounded([shell, "-c", command], s.workdir, timeout, limit)
    except subprocess.TimeoutExpired:
        raise ToolError(f"command timed out after {timeout:g}s and was killed. If it needs longer, run it "
                        "again with background=true.") from None
    if exceeded:
        parts = [f"error: shell output exceeded {limit} bytes; process group stopped",
                 f"exit code {code}"]
    else:
        parts = [f"exit code {code}"]
    if out:
        parts.append("stdout:\n" + out)
    if err:
        parts.append("stderr:\n" + err)
    return "\n".join(parts)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, 9)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _battery_status(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    exe = shutil.which("termux-battery-status")
    if not exe:
        raise ToolError("termux-battery-status not found (needs the termux-api package and Termux:API app)")
    try:
        proc = subprocess.run([exe], capture_output=True, stdin=subprocess.DEVNULL, timeout=15)
    except subprocess.TimeoutExpired:
        raise ToolError("termux-battery-status timed out (is the Termux:API app installed?)") from None
    if proc.returncode != 0:
        raise ToolError(f"termux-battery-status failed: {proc.stderr.decode(errors='replace')[:300]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise ToolError(f"unexpected output: {proc.stdout[:300]!r}") from None
    keep = ("percentage", "status", "plugged", "health", "temperature")
    return json.dumps({k: data[k] for k in keep if k in data})


def _shell_jobs(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    action = args.get("action") or "list"
    try:
        if action == "list":
            return jobs.listing(s)
        if action == "log":
            return jobs.log_text(s, args.get("id"))
        if action == "kill":
            job = jobs.kill(s, args.get("id"))
            return f"job {job['id']}: {jobs.describe(job)}"
    except jobs.JobError as e:
        raise ToolError(str(e)) from None
    raise ToolError("action must be list, log or kill")


def obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


# Risky tools take a `purpose` that the owner sees on the approval prompt, labeled as the model's words.
PURPOSE = {"type": "string", "description": (
    "One plain sentence for the owner, who reads it on a phone before approving: what this does "
    "and why, in everyday words, no jargon. Example: 'Check how full storage is, because you asked "
    "why downloads fail.'")}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "list_dir",
            "List a directory on the phone. Relative paths start at the agent's working directory.",
            obj({"path": {"type": "string", "description": "Directory path; default is the working directory."}}, []),
            _list_dir,
        ),
        Tool(
            "read_file",
            "Read a text file on the phone. Use this instead of cat, head or tail in run_shell. A long file "
            "comes back in pieces: the footer gives the offset to read on from. Also reads saved long output "
            "(paths under ~/.cache/chelus/spill) that other tools point to.",
            obj({"path": {"type": "string"},
                 "offset": {"type": "integer", "description": "First line to read, counting from 1. Default 1."},
                 "limit": {"type": "integer", "description": f"Most lines to return (at most {READ_MAX_LINES})."}},
                ["path"]),
            _read_file,
            spill=False,
        ),
        Tool(
            "write_file",
            "Create or overwrite a text file on the phone. Asks the owner first.",
            obj({"path": {"type": "string"}, "content": {"type": "string"}, "purpose": PURPOSE},
                ["path", "content", "purpose"]),
            _write_file,
            risky=True,
        ),
        Tool(
            "run_shell",
            "Run a shell command in Termux on the phone. Asks the owner first. Waits up to 120 seconds "
            "by default. For anything slower (package installs, deploys, big downloads) set "
            "background=true: it returns a job id at once, runs up to 30 minutes, and the owner is told "
            "when it ends.",
            obj({"command": {"type": "string"},
                 "timeout": {"type": "integer", "description": (
                     "Seconds to wait (default and most 120). More than that runs it in the background.")},
                 "background": {"type": "boolean", "description": "Run without waiting; see shell_jobs."},
                 "purpose": PURPOSE}, ["command", "purpose"]),
            _run_shell,
            risky=True,
        ),
        Tool(
            "shell_jobs",
            "Background shell runs started with run_shell background=true. list: recent jobs and their "
            "state. log: a job's output so far. kill: stop a running job.",
            obj({"action": {"type": "string", "enum": ["list", "log", "kill"]},
                 "id": {"type": "string", "description": "Job id, for log and kill."}}, ["action"]),
            _shell_jobs,
            group="shell_jobs",
        ),
        Tool(
            "battery_status",
            "Read this phone's battery level, charging state and temperature.",
            obj({}, []),
            _battery_status,
        ),
    ]
}


def register(tool: Tool) -> None:
    TOOLS[tool.name] = tool


OWNER_GROUPS = ("skills", "memory", "cron", "web", "shell_jobs")


def available(guest: bool = False, disabled: tuple[str, ...] = ()) -> list[str]:
    """Tool names offered to this kind of user; guests never see the skill or memory tools."""
    return [n for n, t in TOOLS.items() if not (guest and t.group in OWNER_GROUPS) and n not in disabled]


def schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    return [TOOLS[n].schema() for n in (names if names is not None else TOOLS)]


def action_key(s: Settings, name: str, args: dict[str, Any], ctx: Any = None) -> str:
    """Fingerprint actual effects, never purpose or raw secret-bearing grant metadata."""
    effect: dict[str, Any] = {"tool": name, "cwd": os.path.realpath(s.workdir)}
    if name == "run_shell":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        effect.update(command=command, background=shell_mode(s, args)[0])
    elif name == "write_file":
        path = _resolve(s, args.get("path"), ctx)
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        args["path"] = str(path)  # the card, fingerprint and handler use the same real target
        effect.update(path=str(path), content=content)
    elif name == "edit_file":
        if "_diff" not in args:
            raise ToolError("edit not prepared")
        effect.update(path=str(args.get("path")), diff=args["_diff"])
    else:
        raise ToolError("no reusable approval for this tool")
    return hashlib.sha256(json.dumps(effect, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def describe_call(name: str, args: dict[str, Any], s: Settings | None = None) -> approval.Request:
    """What a permission prompt shows: a title written here, the model's stated purpose,
    and the facts (command, path, size). As a string, the old one-line summary."""
    purpose = args.get("purpose") if isinstance(args.get("purpose"), str) else ""
    if name == "run_shell":
        cmd = str(args.get("command"))
        try:
            background = shell_mode(s or Settings(), args)[0]
        except ToolError:
            background = False  # the handler refuses the bad argument after the prompt
        if background:
            return approval.Request(
                f"run_shell (background): {cmd!r}", title="Run a shell command on the phone in the background",
                purpose=purpose, detail=(f"Command:\n{cmd}\n\nRuns in the background for up to "
                                         f"{jobs.LIMIT_S // 60} minutes. You get a message when it ends."),
                choices=(approval.ONCE, approval.SESSION, approval.ALWAYS, approval.DENY))
        return approval.Request(f"run_shell: {cmd!r}", title="Run a shell command on the phone",
                                purpose=purpose, detail=f"Command:\n{cmd}",
                                choices=(approval.ONCE, approval.SESSION, approval.ALWAYS, approval.DENY))
    if name == "write_file":
        content = args.get("content")
        path = str(args.get("path"))
        if isinstance(content, str):
            size, lines = len(content), content.count("\n") + 1
            preview = content[:300] + ("..." if len(content) > 300 else "")
        else:
            size, lines, preview = "?", "?", ""
        try:
            verb = "Overwrite" if Path(os.path.expanduser(path)).exists() else "Create"
        except OSError:
            verb = "Write"
        return approval.Request(
            f"write_file: {path!r} ({size} chars)", title=f"{verb} a file on the phone", purpose=purpose,
            detail=f"File: {path}\nSize: {size} chars, {lines} lines\nStarts with:\n{preview}",
            choices=(approval.ONCE, approval.SESSION, approval.ALWAYS, approval.DENY))
    if name == "edit_file" and "_diff" in args:
        from .edit import card_diff
        path = str(args.get("path"))
        note = f"\n{args['_note']}" if args.get("_note") else ""
        return approval.Request(
            f"edit_file: {path!r}", title="Edit a file on the phone", purpose=purpose,
            detail=f"File: {path}{note}\nChange:\n{card_diff(args['_diff'])}",
            choices=(approval.ONCE, approval.SESSION, approval.ALWAYS, approval.DENY))
    shown = {k: v for k, v in args.items() if not k.startswith("_")}
    return approval.Request(f"{name}({json.dumps(shown)[:200]})", title=f"Use {name}", purpose=purpose)
