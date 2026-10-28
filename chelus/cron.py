"""Scheduled jobs: a stored prompt that runs on a schedule and messages the owner.

The rules follow Hermes Agent's cron (docs/hermes/cron.md has the read and the port list):

- at most once: a fire moves the job past its slot in the same transaction that records
  the run, and the run ledger allows a slot only once. A crash mid-run loses that run; it
  never repeats it. A run left "running" by a dead process is marked unknown, and the owner
  is told it may or may not have finished.
- missed runs collapse: a recurring job more than its grace window late (half its period,
  2 minutes to 2 hours) fires once as a catch-up, never once per missed slot.
- a one-shot more than ONESHOT_GRACE_S late is not run; the owner is told it was missed.
  Hermes uses 120 s because its server is always up. A phone sleeps and Doze delays the
  tick, so a reminder a few minutes late is still worth sending.
- each run is a fresh conversation (origin 'cron') with no chat history. Memory is in the
  prompt, read-only. The model's final reply is the message; [SILENT] sends nothing;
  [CRON_FAILURE] on the first line marks the run failed.
- nobody is there to press approval buttons, so risky tools run only under an existing
  "Always" grant for that exact action (or skill). Everything else is denied, and the
  message lists what was denied.

The Telegram bridge ticks between polls (telegram.Bridge.housekeeping). `chelus cron`
manages jobs from the terminal.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from . import approval, cron_scan, provider, schedule
from .config import Settings
from .store import SessionBusy, Store

ONESHOT_GRACE_S = 1800      # a one-shot later than this is reported missed, not run
LATE_S = 300                # within this, a fire counts as on time (Hermes)
MIN_GRACE_S, MAX_GRACE_S = 120, 7200
RUN_LIMIT_S = 900           # awake seconds a run may take (time.monotonic stops while asleep)
KEEP_RUNS = 1000
KEEP_DONE_S = 7 * 86400     # finished one-shots stay listed this long
OUTPUT_CHARS = 8000         # kept per run, for the owner and for later job chaining
NAME_CHARS = 60
OFF_TOOLS = ("memory", "cron")  # memory is read-only in a run; a job can't schedule jobs

SILENT_MARKERS = ("[SILENT]", "SILENT", "NO_REPLY")
FAILURE_MARKER = "[CRON_FAILURE]"
HINT = (
    "[System: This is a scheduled job, running unattended: nobody is watching this conversation and "
    "you can't ask questions. Your final reply is sent to the owner on Telegram automatically, so write it "
    "as the message itself. If there is nothing worth reporting, reply with exactly [SILENT] and nothing is "
    f"sent. If the job can't be done, put {FAILURE_MARKER} alone on the first line, then say why in one or "
    "two sentences. Risky actions run only if the owner already allowed that exact action with 'Always'; "
    "anything else is denied, so don't retry a denial, and say what you couldn't do. Memory is read-only "
    "here. Don't schedule anything because this task mentions a time.]\n\nThe job:\n"
)


class CronError(ValueError):
    """A job request that can't be done; the message is for the owner."""


# ---- managing jobs ----

def create(store: Store, schedule_text: str, prompt: str, name: str | None = None,
           repeat: int | None = None, catch_up: bool = True, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    prompt = (prompt or "").strip()
    if not prompt:
        raise CronError("a job needs a prompt: what Chelus should do when it runs")
    if blocked := cron_scan.scan(prompt):
        raise CronError(blocked)
    sched = schedule.parse(schedule_text, now=now)
    if sched["kind"] == schedule.ONCE:
        if sched["run_at"] < now - 60:
            raise CronError(f"{schedule.describe(sched['run_at'])} has already passed")
        first: float | None = max(sched["run_at"], now)
    else:
        first = schedule.next_run(sched, now)
        if first is None:
            raise CronError(f"{sched['display']!r} never comes round")
    if repeat is not None and repeat <= 0:
        repeat = None  # 0 or less: forever, as in Hermes
    if sched["kind"] == schedule.ONCE:
        repeat = 1
    job = {"id": secrets.token_hex(6), "name": (name or prompt).strip().splitlines()[0][:NAME_CHARS],
           "prompt": prompt, "schedule": sched, "repeat_times": repeat, "catch_up": 1 if catch_up else 0,
           "state": "scheduled", "next_run_at": first, "created": now}
    store.cron_add(job)
    return store.cron_job(job["id"])


def resolve(store: Store, ref: str) -> dict[str, Any]:
    """A job by id, id prefix or exact name."""
    ref = (ref or "").strip()
    jobs = store.cron_jobs()
    exact = [j for j in jobs if j["id"] == ref]
    matches = exact or [j for j in jobs if ref and j["id"].startswith(ref)] or [j for j in jobs if j["name"] == ref]
    if not matches:
        raise CronError(f"no job {ref!r}; list the jobs to see their ids")
    if len(matches) > 1:
        raise CronError(f"{ref!r} matches {len(matches)} jobs: " + ", ".join(j["id"] for j in matches))
    return matches[0]


def update(store: Store, ref: str, schedule_text: str | None = None, prompt: str | None = None,
           name: str | None = None, repeat: int | None = None, catch_up: bool | None = None,
           now: float | None = None) -> dict[str, Any]:
    """Change the parts of a job that are given. A new schedule moves the next run to its first
    slot after now, unless the job is paused."""
    now = time.time() if now is None else now
    job = resolve(store, ref)
    if job["state"] == "completed":
        raise CronError(f"job {job['id']} has finished; create a new one")
    fields: dict[str, Any] = {}
    if prompt is not None:
        prompt = prompt.strip()
        if not prompt:
            raise CronError("a job needs a prompt: what Chelus should do when it runs")
        if blocked := cron_scan.scan(prompt):
            raise CronError(blocked)
        fields["prompt"] = prompt
    if name is not None:
        fields["name"] = (name.strip().splitlines() or [""])[0][:NAME_CHARS] or job["name"]
    if catch_up is not None:
        fields["catch_up"] = 1 if catch_up else 0
    if schedule_text is not None:
        sched = schedule.parse(schedule_text, now=now)
        if sched["kind"] == schedule.ONCE:
            if sched["run_at"] < now - 60:
                raise CronError(f"{schedule.describe(sched['run_at'])} has already passed")
            first: float | None = max(sched["run_at"], now)
            fields["repeat_times"] = 1
        else:
            first = schedule.next_run(sched, now)
            if first is None:
                raise CronError(f"{sched['display']!r} never comes round")
        fields["schedule"] = sched
        if job["state"] in ("scheduled", "error"):
            fields.update(next_run_at=first, state="scheduled")
        elif job["state"] == "paused":
            fields["next_run_at"] = first
    if repeat is not None and "repeat_times" not in fields:
        fields["repeat_times"] = repeat if repeat > 0 else None
    if not fields:
        raise CronError("nothing to change: give a new schedule, prompt, name, repeat or catch_up")
    store.cron_update(job["id"], **fields)
    return store.cron_job(job["id"])


def trigger(store: Store, ref: str, now: float | None = None) -> dict[str, Any]:
    """Make a scheduled job due now; the next tick (the Telegram bridge) runs it. A recurring
    job keeps its schedule afterwards."""
    job = resolve(store, ref)
    if job["state"] != "scheduled":
        raise CronError(f"job {job['id']} is {job['state']}; only a scheduled job can be run now"
                        + (" (resume it first)" if job["state"] in ("paused", "error") else ""))
    store.cron_update(job["id"], next_run_at=time.time() if now is None else now)
    return store.cron_job(job["id"])


def pause(store: Store, ref: str) -> dict[str, Any]:
    job = resolve(store, ref)
    if job["state"] != "scheduled":
        raise CronError(f"job {job['id']} is {job['state']}, not scheduled")
    store.cron_update(job["id"], state="paused")
    return store.cron_job(job["id"])


def resume(store: Store, ref: str, now: float | None = None) -> dict[str, Any]:
    """A slot that came due while paused stays due, so it fires on the next tick."""
    job = resolve(store, ref)
    if job["state"] not in ("paused", "error"):
        raise CronError(f"job {job['id']} is {job['state']}; only paused or failed jobs resume")
    fields: dict[str, Any] = {"state": "scheduled"}
    if job["next_run_at"] is None:
        nxt = schedule.next_run(job["schedule"], time.time() if now is None else now) if job["schedule"] else None
        if nxt is None:
            raise CronError(f"job {job['id']} has no run left to resume")
        fields["next_run_at"] = nxt
    store.cron_update(job["id"], **fields)
    return store.cron_job(job["id"])


def remove(store: Store, ref: str) -> dict[str, Any]:
    job = resolve(store, ref)
    store.cron_remove(job["id"])
    return job


def describe_job(job: dict[str, Any]) -> str:
    sched = job["schedule"].get("display", "?") if job["schedule"] else "(bad schedule)"
    line = f"{job['id']}  {job['name']}\n    {sched}; {job['state']}"
    if job["state"] == "scheduled":
        line += f", next {schedule.describe(job['next_run_at'])}"
    if job["repeat_times"]:
        line += f", run {job['repeat_done']} of {job['repeat_times']}"
    if job["last_run_at"]:
        line += f"\n    last {schedule.describe(job['last_run_at'])}: {job['last_status']}"
        if job["last_error"]:
            line += f" ({job['last_error'][:120]})"
    return line


# ---- deciding what is due ----

@dataclass
class Plan:
    action: str                 # fire, skip (catch-up off), retire (one-shot missed), error
    kind: str = "on_time"       # on_time, late, catch_up
    next_run_at: float | None = None
    state: str = "scheduled"
    reason: str = ""


def grace_seconds(sched: dict[str, Any], around: float) -> float:
    period = schedule.period_seconds(sched, around)
    if not period:
        return MIN_GRACE_S
    return max(MIN_GRACE_S, min(period / 2, MAX_GRACE_S))


def plan(job: dict[str, Any], now: float) -> Plan:
    """What to do with a due job. Pure: the tick applies it."""
    sched, slot = job["schedule"], float(job["next_run_at"])
    late = now - slot
    if sched.get("kind") == schedule.ONCE:
        if late > ONESHOT_GRACE_S:
            return Plan("retire", "missed", None, "completed",
                        f"due {schedule.describe(slot)}, {late / 60:.0f} minutes before Chelus could run it")
        return Plan("fire", "on_time" if late <= LATE_S else "late", None, "completed")
    if not schedule.recurring(sched):
        return Plan("error", state="error", reason="the schedule is unreadable")
    grace = grace_seconds(sched, slot)
    kind = "on_time" if late <= LATE_S else "late" if late <= grace else "catch_up"
    nxt = schedule.next_run(sched, slot)
    if nxt is not None and nxt <= now:
        nxt = schedule.next_run(sched, now)  # collapse the backlog: one run now, then back on schedule
    state, reason = "scheduled", ""
    if nxt is None:
        state, reason = "error", "the schedule has no run after this one"
    times, done = job["repeat_times"], int(job["repeat_done"] or 0)
    if times is not None and done + 1 >= times:
        nxt, state = None, "completed"
    if kind == "catch_up" and not job["catch_up"]:
        return Plan("skip", kind, nxt, "scheduled" if nxt is not None else state,
                    f"missed {schedule.describe(slot)} and catch-up is off")
    return Plan("fire", kind, nxt, state, reason)


# ---- running ----

@dataclass
class Result:
    job_id: str
    name: str
    status: str                  # ok, failed, missed, skipped
    run_id: int | None = None
    text: str = ""               # what was (or would have been) sent
    delivery: str = "none"
    error: str = ""
    denied: list[str] = field(default_factory=list)


Deliver = Callable[[str], None]  # sends one message to the owner; raises if it can't


class Runner:
    """Runs one job as a fresh, unattended agent turn and reports it to the owner."""

    def __init__(self, s: Settings, api_key: str, store: Store, deliver: Deliver | None = None,
                 log: Callable[[str], None] = print, chat_fn: Callable[..., dict[str, Any]] = provider.chat,
                 clock: Callable[[], float] = time.monotonic):
        self.s, self.api_key, self.store = s, api_key, store
        self.deliver, self.log, self.chat_fn, self.clock = deliver, log, chat_fn, clock

    def run(self, job: dict[str, Any], run_id: int, kind: str) -> Result:
        from .agent import Agent  # agent imports the tool modules; keep cron light to import

        res = Result(job["id"], job["name"], "failed", run_id)
        sid: int | None = None
        reply = ""
        started = self.clock()

        def approve(req: str) -> str:
            res.denied.append(getattr(req, "title", "") or str(req))
            self.log(f"[cron] {job['id']}: denied while unattended: {req}")
            return approval.DENY

        def on_event(event: str, detail: str) -> None:
            if event in ("retry", "wait", "guard", "notice", "denied"):
                self.log(f"[cron] {job['id']} {event}: {detail}")
            if event in ("tool", "usage") and self.clock() - started > RUN_LIMIT_S:
                raise provider.ProviderError(f"the run took longer than its {RUN_LIMIT_S // 60}-minute limit")

        try:
            if blocked := cron_scan.scan(job["prompt"]):  # a prompt edited into the database by hand
                raise provider.ProviderError(blocked)
            sid = self.store.new_session()
            self.store.set_session_field(sid, "origin", "cron")
            self.store.set_session_field(sid, "title", f"cron: {job['name']}"[:60])
            agent = Agent(self.s, self.api_key, self.store, sid, approve, on_event, chat_fn=self.chat_fn,
                          hard_stop=True, actor="cron")
            agent.tool_names = [n for n in agent.tool_names if n not in OFF_TOOLS]
            reply = agent.run_turn(job["prompt"], api_text=HINT + job["prompt"])
            res.status, res.text = _read_reply(reply)
            if res.status == "failed":
                res.error = res.text.splitlines()[0][:300] if res.text else "the job reported a failure"
        except (provider.ProviderError, SessionBusy) as e:
            res.error = str(e)
        except Exception as e:  # a crash in one job must not stop the bridge or the other jobs
            res.error = f"crashed: {type(e).__name__}: {e}"
            self.log(f"[cron] {job['id']} crashed:\n{traceback.format_exc()}")
        message = self._message(job, kind, res)
        res.delivery = self._send(job, message) if message else "silent"
        res.text = message or res.text
        self._finish(job, res, sid, reply)
        return res

    def _message(self, job: dict[str, Any], kind: str, res: Result) -> str:
        """The owner's message, or "" for a silent success. Failures always speak."""
        label = f"[scheduled] {job['name']}"
        if kind in ("late", "catch_up"):
            label += " (ran late: the phone or Chelus was asleep or off at the scheduled time)"
        if res.status == "silent":
            return ""
        if res.status == "failed":
            body = f"{label}\nFailed: {res.error}"
            nxt = (self.store.cron_job(job["id"]) or {}).get("next_run_at")
            if nxt:
                body += f"\nNext try: {schedule.describe(nxt)}."
        else:
            body = f"{label}\n{res.text}"
        if res.denied:
            body += ("\n\nDenied, because nobody was there to approve: " + "; ".join(dict.fromkeys(res.denied))
                     + ". To let this job do it, run the same action once in chat and choose 'Always'.")
        return body

    def _send(self, job: dict[str, Any], message: str) -> str:
        if self.deliver is None:
            return "none"
        try:
            self.deliver(message)
            return "sent"
        except Exception as e:  # delivery has its own status; it is not a job failure
            self.log(f"[cron] {job['id']}: could not deliver: {type(e).__name__}: {e}")
            return "failed"

    def _finish(self, job: dict[str, Any], res: Result, sid: int | None, reply: str) -> None:
        status = "ok" if res.status in ("ok", "silent") else "failed"
        try:
            self.store.cron_finish_run(res.run_id, status=status, session_id=sid, error=res.error or None,
                                       delivery=res.delivery, output=(reply or "")[:OUTPUT_CHARS])
            current = self.store.cron_job(job["id"])
            if current is not None:
                streak = 0 if status == "ok" else int(current["failure_streak"] or 0) + 1
                self.store.cron_update(job["id"], last_run_at=time.time(), last_status=status,
                                       last_error=res.error or None, failure_streak=streak)
        except sqlite3.Error as e:
            self.log(f"[cron] {job['id']}: could not record the run: {e}")


def _read_reply(reply: str) -> tuple[str, str]:
    """(status, text): silent when a marker is the whole reply or its first or last line;
    failed when [CRON_FAILURE] opens it."""
    text = (reply or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].upper().startswith(FAILURE_MARKER):
        rest = lines[0][len(FAILURE_MARKER):].strip()
        return "failed", "\n".join(([rest] if rest else []) + lines[1:])
    if not lines or lines[0].upper() in SILENT_MARKERS or lines[-1].upper() in SILENT_MARKERS:
        return ("silent", "") if lines else ("failed", "the model returned an empty reply")
    return "ok", text


# ---- the tick ----

def tick(store: Store, runner: Runner, now: float | None = None) -> list[Result]:
    """Fire every due job, one after another. A bad job is logged and marked, never fatal."""
    results = []
    for job_id in store.cron_due(time.time() if now is None else now):
        try:
            r = fire(store, runner, job_id, now)
        except Exception as e:  # keep scanning the others
            runner.log(f"[cron] {job_id}: {type(e).__name__}: {e}")
            try:
                store.cron_update(job_id, state="error", last_error=f"{type(e).__name__}: {e}"[:300])
            except sqlite3.Error:
                pass
            continue
        if r is not None:
            results.append(r)
    try:
        store.cron_prune(KEEP_RUNS, time.time() - KEEP_DONE_S)
    except sqlite3.Error:
        pass
    return results


def fire(store: Store, runner: Runner, job_id: str, now: float | None = None) -> Result | None:
    """Claim a due job's slot and run it. None when it wasn't due or someone else claimed it."""
    now = time.time() if now is None else now
    job = store.cron_job(job_id)
    if job is None or job["state"] != "scheduled" or job["next_run_at"] is None or job["next_run_at"] > now:
        return None
    slot = float(job["next_run_at"])
    p = plan(job, now)
    if p.action == "error":
        store.cron_update(job_id, state="error", last_error=p.reason)
        runner.log(f"[cron] {job_id}: {p.reason}")
        return None
    if p.action == "skip":
        store.cron_update(job_id, next_run_at=p.next_run_at, state=p.state)
        runner.log(f"[cron] {job_id}: skipped, {p.reason}")
        return Result(job_id, job["name"], "skipped", error=p.reason)
    if p.action == "retire":
        run_id = store.cron_claim(job_id, slot, "missed", next_run_at=None, state="completed",
                                  bump=False, status="missed", error=p.reason)
        if run_id is None:
            return None
        res = Result(job_id, job["name"], "missed", run_id, error=p.reason)
        res.text = f"[scheduled] {job['name']}\nMissed and not run: it was {p.reason}."
        res.delivery = runner._send(job, res.text)
        store.cron_finish_run(run_id, status="missed", delivery=res.delivery)
        store.cron_update(job_id, last_status="missed", last_error=p.reason)
        return res
    run_id = store.cron_claim(job_id, slot, p.kind, next_run_at=p.next_run_at, state=p.state, bump=True)
    if run_id is None:
        # The ledger already has this slot (say, a hand-edited next_run_at). Never run it again;
        # move the job past it so the tick doesn't keep finding it due.
        current = store.cron_job(job_id)
        if current and current["state"] == "scheduled" and current["next_run_at"] == slot:
            store.cron_update(job_id, next_run_at=p.next_run_at, state=p.state)
        return None
    if p.reason:
        store.cron_update(job_id, last_error=p.reason)
    runner.log(f"[cron] {job_id}: running ({p.kind}) {job['name']!r}")
    return runner.run(job, run_id, p.kind)


def run_now(store: Store, runner: Runner, ref: str) -> Result:
    """Run a job immediately, outside its schedule; the schedule is left alone."""
    job = resolve(store, ref)
    return runner.run(job, store.cron_start_manual(job["id"]), "manual")


def recover(store: Store, runner: Runner) -> list[dict[str, Any]]:
    """At startup: close runs a dead process left open, and tell the owner."""
    orphans = store.cron_orphans()
    for run in orphans:
        why = "Chelus stopped during this run, so it may or may not have finished"
        store.cron_finish_run(run["id"], status="unknown", error=why)
        job = store.cron_job(run["job_id"])
        name = job["name"] if job else run["job_id"]
        if job is not None:
            store.cron_update(job["id"], last_status="unknown", last_error=why)
        runner._send(job or {"id": run["job_id"]},
                     f"[scheduled] {name}\nInterrupted: {why}. Check its effects before relying on it.")
    return orphans


def seconds_until_due(store: Store, now: float | None = None) -> float | None:
    nxt = store.cron_next_due()
    if nxt is None:
        return None
    return max(0.0, nxt - (time.time() if now is None else now))
