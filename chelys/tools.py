"""Phone-local tools. Each one has an OpenAI-style schema, a handler and a risk flag.

Risky tools (writing files, running shell commands) go through the agent's permission
callback before they run. Arbitrary shell access is not a sandbox; the gate is the safety.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings


class ToolError(Exception):
    """A tool failure the model should see and can react to."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[Settings, dict[str, Any]], str]
    risky: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _resolve(s: Settings, path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string")
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path(s.workdir) / p
    return p


def _list_dir(s: Settings, args: dict[str, Any]) -> str:
    p = _resolve(s, args.get("path") or ".")
    if not p.is_dir():
        raise ToolError(f"not a directory: {p}")
    rows = []
    for entry in sorted(p.iterdir(), key=lambda e: e.name)[:200]:
        try:
            if entry.is_dir():
                rows.append(f"{entry.name}/")
            else:
                rows.append(f"{entry.name}  {entry.stat().st_size} bytes")
        except OSError:
            rows.append(f"{entry.name}  (unreadable)")
    return f"{p}\n" + ("\n".join(rows) if rows else "(empty)")


def _read_file(s: Settings, args: dict[str, Any]) -> str:
    p = _resolve(s, args.get("path"))
    if not p.is_file():
        raise ToolError(f"not a file: {p}")
    with p.open("rb") as f:
        data = f.read(s.read_file_bytes + 1)
    text = data[: s.read_file_bytes].decode("utf-8", errors="replace")
    if len(data) > s.read_file_bytes:
        text += f"\n... [stopped after {s.read_file_bytes} bytes]"
    return text


def _write_file(s: Settings, args: dict[str, Any]) -> str:
    p = _resolve(s, args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if p.is_dir():
        raise ToolError(f"is a directory: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content.encode('utf-8'))} bytes to {p}"


def _run_shell(s: Settings, args: dict[str, Any]) -> str:
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
    try:
        raw_out, raw_err = proc.communicate(timeout=s.shell_timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise ToolError(f"command timed out after {s.shell_timeout:g}s and was killed") from None
    except BaseException:
        _kill_group(proc)
        raise
    out = raw_out.decode("utf-8", errors="replace")
    err = raw_err.decode("utf-8", errors="replace")
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
        proc.communicate(timeout=5)
    except Exception:
        pass


def _battery_status(s: Settings, args: dict[str, Any]) -> str:
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


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "list_dir",
            "List a directory on the phone. Relative paths start at the agent's working directory.",
            _obj({"path": {"type": "string", "description": "Directory path; default is the working directory."}}, []),
            _list_dir,
        ),
        Tool(
            "read_file",
            "Read a text file on the phone (large files are cut off).",
            _obj({"path": {"type": "string"}}, ["path"]),
            _read_file,
        ),
        Tool(
            "write_file",
            "Create or overwrite a text file on the phone. Asks the owner first.",
            _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
            _write_file,
            risky=True,
        ),
        Tool(
            "run_shell",
            "Run a shell command in Termux on the phone, with a timeout. Asks the owner first.",
            _obj({"command": {"type": "string"}}, ["command"]),
            _run_shell,
            risky=True,
        ),
        Tool(
            "battery_status",
            "Read this phone's battery level, charging state and temperature.",
            _obj({}, []),
            _battery_status,
        ),
    ]
}


def schemas() -> list[dict[str, Any]]:
    return [t.schema() for t in TOOLS.values()]


def describe_call(name: str, args: dict[str, Any]) -> str:
    """Short human-readable summary for permission prompts and logs."""
    if name == "run_shell":
        return f"run_shell: {args.get('command')!r}"
    if name == "write_file":
        content = args.get("content")
        size = len(content) if isinstance(content, str) else "?"
        return f"write_file: {args.get('path')!r} ({size} chars)"
    return f"{name}({json.dumps(args)[:200]})"
