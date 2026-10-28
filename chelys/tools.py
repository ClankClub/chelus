"""Phone-local tools. Each one has an OpenAI-style schema, a handler and a risk flag.

Risky tools (writing files, running shell commands) go through the agent's permission
callback before they run. Arbitrary shell access is not a sandbox; the gate is the safety.

File tools never touch Chelys's own config and data directories (API key, bot token,
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

from . import approval
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
            raise ToolError(f"{p} is off limits: that directory holds Chelys's keys, settings, history "
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


def _read_file(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    p = _resolve(s, args.get("path"), ctx)
    if not p.is_file():
        raise ToolError(f"not a file: {p}")
    with p.open("rb") as f:
        data = f.read(s.read_file_bytes + 1)
    text = data[: s.read_file_bytes].decode("utf-8", errors="replace")
    if len(data) > s.read_file_bytes:
        text += f"\n... [stopped after {s.read_file_bytes} bytes]"
    return text


def _write_file(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    p = _resolve(s, args.get("path"), ctx)
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if p.is_dir():
        raise ToolError(f"is a directory: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content.encode('utf-8'))} bytes to {p}"


def _run_shell(s: Settings, args: dict[str, Any], ctx: Any = None) -> str:
    import selectors
    import time

    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")
    shell = shutil.which("bash") or shutil.which("sh") or "/system/bin/sh"
    # Own process group, so a timeout or Ctrl-C kills the command and anything it spawned.
    proc = subprocess.Popen(
        [shell, "-c", command],
        cwd=s.workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    total, limit, exceeded = 0, 65536, False
    deadline = time.monotonic() + s.shell_timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in buffers:
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, s.shell_timeout)
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
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise ToolError(f"command timed out after {s.shell_timeout:g}s and was killed") from None
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        for pipe in buffers:
            pipe.close()
    out = buffers[proc.stdout].decode("utf-8", errors="replace")
    err = buffers[proc.stderr].decode("utf-8", errors="replace")
    if exceeded:
        parts = [f"error: shell output exceeded {limit} bytes; process group stopped",
                 f"exit code {proc.returncode}"]
    else:
        parts = [f"exit code {proc.returncode}"]
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
            "Read a text file on the phone (large files are cut off).",
            obj({"path": {"type": "string"}}, ["path"]),
            _read_file,
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
            "Run a shell command in Termux on the phone, with a timeout. Asks the owner first.",
            obj({"command": {"type": "string"}, "purpose": PURPOSE}, ["command", "purpose"]),
            _run_shell,
            risky=True,
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


OWNER_GROUPS = ("skills", "memory")


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
        effect.update(command=command, timeout=s.shell_timeout)
    elif name == "write_file":
        path = _resolve(s, args.get("path"), ctx)
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        args["path"] = str(path)  # the card, fingerprint and handler use the same real target
        effect.update(path=str(path), content=content)
    else:
        raise ToolError("no reusable approval for this tool")
    return hashlib.sha256(json.dumps(effect, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def describe_call(name: str, args: dict[str, Any]) -> approval.Request:
    """What a permission prompt shows: a title written here, the model's stated purpose,
    and the facts (command, path, size). As a string, the old one-line summary."""
    purpose = args.get("purpose") if isinstance(args.get("purpose"), str) else ""
    if name == "run_shell":
        cmd = str(args.get("command"))
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
    return approval.Request(f"{name}({json.dumps(args)[:200]})", title=f"Use {name}", purpose=purpose)
