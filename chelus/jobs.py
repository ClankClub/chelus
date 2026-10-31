"""Background shell runs (Hermes's process registry, docs/hermes/tools.md item 2).

`run_shell` with background=true, or with a timeout past the foreground limit, starts the
command here and returns a job id at once. A small worker process (this file, run as a
script) runs it in its own process group, writes its output to a capped log, stops it at
the 30-minute limit, and writes the result. The worker is its own session, so it outlives
the `chelus` process that started it and a bridge restart.

Files live in the data folder, which the file tools never open: shell_jobs/<id>.json (the
command), .log (output: the first 64 KB, then a rolling tail, 256 KB in all), .pid (the
command's process group), .done (exit code and reason), .killed (a stop request) and .told
(the finish notice was claimed, so the bridge and a terminal chat never both send it).

The Telegram bridge checks between polls and sends the owner one message per finished
job; the message is also kept as a note for the owner's next turn, as cron's are. A
terminal chat prints the notice before the next turn instead.

This module imports only the standard library at the top, so the worker can run it by path.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

LIMIT_S = 1800                # a background run is stopped after this long
HEAD_BYTES = 64 * 1024        # the log keeps the start of the output...
LOG_BYTES = 256 * 1024        # ...and the latest output, this much in all
SLACK_BYTES = 32 * 1024       # the tail is trimmed only once it is this far over
KEEP_S = 7 * 86400            # finished jobs are deleted after a week
KEEP_JOBS = 30                # and only this many finished jobs are kept
MAX_RUNNING = 3
NOTICE_LINES, NOTICE_CHARS = 15, 1200
ID_CHARS = 6


class JobError(Exception):
    """A request the model can correct: unknown id, too many jobs, and the like."""


def jobs_dir(s) -> Path:
    return Path(s.data_dir) / "shell_jobs"


def _folder(s) -> Path:
    folder = jobs_dir(s)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    return folder


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "posix":  # os.kill(pid, 0) would end the process on Windows; trust the time limit
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---- starting and stopping ----

def start(s, command: str, purpose: str = "") -> dict[str, Any]:
    """Start command in the background. Returns the job as status() describes it."""
    folder = _folder(s)
    prune(s)
    running = [j for j in all_jobs(s) if j["state"] == "running"]
    if len(running) >= MAX_RUNNING:
        raise JobError(f"{len(running)} background jobs are already running "
                       f"({', '.join(j['id'] for j in running)}). Wait for one to finish or stop one "
                       "with shell_jobs action=kill.")
    job_id = secrets.token_hex(ID_CHARS // 2)
    meta = {"id": job_id, "command": command, "purpose": (purpose or "")[:300], "cwd": s.workdir,
            "started": time.time(), "limit": LIMIT_S, "pid": None}
    _write_json(folder / f"{job_id}.json", meta)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), str(folder), job_id],
        cwd=s.workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    # Reap the worker when it exits, so a dead worker never looks alive as a zombie.
    threading.Thread(target=proc.wait, daemon=True).start()
    meta["pid"] = proc.pid
    _write_json(folder / f"{job_id}.json", meta)
    return status(s, job_id)


def kill(s, job_id: str) -> dict[str, Any]:
    job = status(s, job_id)
    if job["state"] != "running":
        raise JobError(f"job {job['id']} is not running ({describe(job)})")
    folder = jobs_dir(s)
    (folder / f"{job['id']}.killed").touch()
    try:
        pgid = int((folder / f"{job['id']}.pid").read_text().strip())
    except (OSError, ValueError):
        raise JobError(f"job {job['id']} is still starting; try again in a moment") from None
    try:
        if os.name == "posix":
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # The worker sees the command end and writes the result; give it a moment.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (folder / f"{job['id']}.done").exists():
        time.sleep(0.1)
    return status(s, job["id"])


# ---- reading state ----

def _resolve(s, ref: Any) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise JobError("id must be a job id, like the one run_shell returned")
    ref = ref.strip().lower()
    ids = [p.stem for p in jobs_dir(s).glob("*.json")] if jobs_dir(s).is_dir() else []
    matches = [i for i in ids if i.startswith(ref)]
    if len(matches) != 1:
        raise JobError(f"no background job {ref!r}" if not matches else f"{ref!r} matches several jobs")
    return matches[0]


def status(s, ref: Any, now: float | None = None) -> dict[str, Any]:
    job_id = _resolve(s, ref)
    folder = jobs_dir(s)
    meta = _read_json(folder / f"{job_id}.json")
    if meta is None:
        raise JobError(f"job {job_id} can't be read")
    now = time.time() if now is None else now
    job = dict(meta, id=job_id)
    done = _read_json(folder / f"{job_id}.done")
    if done is not None:
        job.update(state="done", code=done.get("code"), reason=done.get("reason", "exit"),
                   ended=done.get("ended"), error=done.get("error"))
    else:
        started = float(meta.get("started") or 0)
        limit = float(meta.get("limit") or LIMIT_S)
        young = now - started < 30
        if (young and meta.get("pid") is None) or (_alive(meta.get("pid")) and now - started < limit + 300):
            job.update(state="running")
        else:
            job.update(state="done", code=None, reason="lost", ended=None)
    job["told"] = (folder / f"{job_id}.told").exists()
    return job


def all_jobs(s) -> list[dict[str, Any]]:
    folder = jobs_dir(s)
    if not folder.is_dir():
        return []
    out = []
    for p in folder.glob("*.json"):
        try:
            out.append(status(s, p.stem))
        except JobError:
            continue
    return sorted(out, key=lambda j: j.get("started") or 0, reverse=True)


def any_running(s) -> bool:
    return any(j["state"] == "running" for j in all_jobs(s))


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def describe(job: dict[str, Any], now: float | None = None) -> str:
    now = time.time() if now is None else now
    started = float(job.get("started") or now)
    if job["state"] == "running":
        return f"running for {_duration(now - started)}"
    took = f" after {_duration(job['ended'] - started)}" if job.get("ended") else ""
    reason = job.get("reason")
    if reason == "exit":
        return f"finished with exit code {job.get('code')}{took}"
    if reason == "timeout":
        return f"stopped at the {LIMIT_S // 60}-minute limit"
    if reason == "killed":
        return f"stopped on request{took}"
    if reason == "error":
        return f"could not start: {job.get('error')}"
    return "ended without a result (Chelus or the phone probably restarted while it ran)"


def _short(command: str, n: int = 120) -> str:
    one = " ".join(command.split())
    return one if len(one) <= n else one[:n - 3] + "..."


def read_log(s, job_id: str) -> str:
    try:
        raw = (jobs_dir(s) / f"{job_id}.log").read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _redact(text: str) -> str:
    from . import compaction  # imported here so the worker can run this file by path
    return compaction.redact(text, keep_lines=True)


def listing(s, now: float | None = None) -> str:
    jobs = all_jobs(s)[:10]
    if not jobs:
        return "No background jobs."
    return "\n".join(f"{j['id']}: {describe(j, now)}: {_short(j['command'], 80)}" for j in jobs)


def log_text(s, ref: Any) -> str:
    job = status(s, ref)
    body = _redact(read_log(s, job["id"])) or "(no output yet)"
    return f"Job {job['id']}, {describe(job)}.\nCommand: {_short(job['command'], 300)}\nOutput:\n{body}"


# ---- finish notices ----

def notice(s, job: dict[str, Any]) -> str:
    lines = [f"[background job {job['id']}] {describe(job)}.", f"Command: {_short(job['command'])}"]
    if job.get("purpose"):
        lines.append(f"Why: {_short(job['purpose'], 200)}")
    tail = _redact(read_log(s, job["id"])).rstrip().splitlines()[-NOTICE_LINES:]
    text = "\n".join(tail)
    if len(text) > NOTICE_CHARS:
        text = "..." + text[-NOTICE_CHARS:]
    if text:
        lines += ["Last output:", text]
    return "\n".join(lines)


def _claim(s, job_id: str) -> bool:
    try:
        fd = os.open(jobs_dir(s) / f"{job_id}.told", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    os.close(fd)
    return True


def collect(s) -> list[str]:
    """Notices for finished jobs nobody has been told about, claimed so they come once."""
    out = []
    for job in reversed(all_jobs(s)):  # oldest first
        if job["state"] == "done" and not job["told"] and _claim(s, job["id"]):
            out.append(notice(s, job))
    return out


def notify(s, deliver: Callable[[str], Any], note: Callable[[str], None] | None = None,
           log: Callable[[str], None] = print) -> int:
    """Send each new finish notice with deliver (which returns a message id) and keep it as a
    note for the owner's next turn. A failed send is logged and not retried, as cron's are;
    the note and shell_jobs still have it."""
    sent = 0
    for text in collect(s):
        try:
            if deliver(text):
                sent += 1
            else:
                log("[jobs] finish notice not confirmed: no message id came back")
        except Exception as e:
            log(f"[jobs] could not send a finish notice: {type(e).__name__}: {e}")
        if note is not None:
            try:
                note(text)
            except Exception as e:
                log(f"[jobs] could not keep the notice for the next turn: {e}")
    return sent


def prune(s, now: float | None = None) -> None:
    """Delete finished jobs older than a week, and all but the newest KEEP_JOBS finished ones."""
    now = time.time() if now is None else now
    done = [j for j in all_jobs(s) if j["state"] == "done"]
    for i, job in enumerate(done):
        if i >= KEEP_JOBS or now - float(job.get("started") or 0) > KEEP_S:
            for p in jobs_dir(s).glob(f"{job['id']}.*"):
                try:
                    p.unlink()
                except OSError:
                    pass


# ---- the worker ----

class _Log:
    """The output file: the first HEAD_BYTES as they came, then the latest output, about
    LOG_BYTES in all, with a marker where the middle was dropped."""

    def __init__(self, path: Path):
        self.f = open(path, "wb")
        self.head = 0
        self.tail = bytearray()
        self.dropped = 0
        self.keep = LOG_BYTES - HEAD_BYTES

    def write(self, chunk: bytes) -> None:
        if self.head < HEAD_BYTES:
            take = chunk[:HEAD_BYTES - self.head]
            self.f.write(take)
            self.head += len(take)
            chunk = chunk[len(take):]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.keep + SLACK_BYTES:
                cut = len(self.tail) - self.keep
                del self.tail[:cut]
                self.dropped += cut
                self.f.seek(self.head)
                self.f.write(f"\n[... {self.dropped} bytes of output dropped here ...]\n".encode())
                self.f.write(self.tail)
                self.f.truncate()
            else:
                self.f.write(chunk)
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def _work(folder: Path, job_id: str) -> None:
    import selectors

    meta = _read_json(folder / f"{job_id}.json") or {}
    started = time.monotonic()

    def finish(code: Any, reason: str, error: str = "") -> None:
        if (folder / f"{job_id}.killed").exists() and reason == "exit":
            reason = "killed"
        _write_json(folder / f"{job_id}.done",
                    {"code": code, "reason": reason, "error": error[:300], "ended": time.time()})

    command = meta.get("command")
    if not isinstance(command, str):
        finish(None, "error", "job file unreadable")
        return
    shell = shutil.which("bash") or shutil.which("sh") or "/system/bin/sh"
    try:
        child = subprocess.Popen([shell, "-c", command], cwd=meta.get("cwd") or None,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=(os.name == "posix"))
    except OSError as e:
        finish(None, "error", str(e))
        return
    (folder / f"{job_id}.pid").write_text(str(child.pid))
    deadline = started + float(meta.get("limit") or LIMIT_S)
    log = _Log(folder / f"{job_id}.log")
    reason, exited_at = "exit", None
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(child.stdout, selectors.EVENT_READ)
            while sel.get_map():
                now = time.monotonic()
                if now >= deadline:
                    reason = "timeout"
                    break
                if exited_at is None and child.poll() is not None:
                    exited_at = now
                # Something the command left running may hold the pipe open; stop reading
                # shortly after the command itself has exited.
                if exited_at is not None and now - exited_at > 2:
                    break
                for key, _ in sel.select(min(1.0, deadline - now)):
                    chunk = key.fileobj.read1(65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        break
                    log.write(chunk)
        if reason == "timeout":
            _stop(child)
        else:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _stop(child)
                reason = "timeout"
    finally:
        log.close()
        child.stdout.close()
    finish(child.returncode, reason)


def _stop(child: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


if __name__ == "__main__":
    _work(Path(sys.argv[1]), sys.argv[2])
