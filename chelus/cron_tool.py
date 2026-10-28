"""The `cron` tool: the model schedules, lists, changes and runs the owner's jobs from chat.

Rebuilt from Hermes Agent's cronjob_tools.py (Nous Research, MIT); docs/hermes/cron.md has the
read. One tool with an action argument keeps the schema small. Choices made here:

- owner only, and only in the owner's own foreground conversation. A guest never sees it,
  and a scheduled run, the background review and the memory review don't have it, so a job
  can't create jobs and injected text in a run can't plant new ones.
- every action that starts, changes or removes unattended work asks the owner first, with
  once-or-deny only: "Always" would turn one yes into a standing licence. list and pause don't ask.
- the prompt is scanned (cron_scan) before the owner is asked, so they never see a prompt
  Chelus would refuse.
- `run` makes the job due now and returns; the Telegram bridge's next tick runs it and
  messages the owner. Nothing waits in the conversation for it.
"""

from __future__ import annotations

from typing import Any

from . import approval, cron, cron_scan, schedule, tools

ACTIONS = ("create", "list", "update", "pause", "resume", "remove", "run")
GATED = ("create", "update", "resume", "remove", "run")
PROMPT_SHOWN = 700

SCHEDULE_HELP = (
    "Schedule forms: (1) recurring interval: '30m', 'every 2h', 'every hour' (forever unless repeat is set); "
    "(2) one-shot by duration: 'in 30m', 'in 2h' (fires once that far from now; use this for 'remind me in N "
    "minutes' and do NOT work out a timestamp yourself); (3) natural day and time: 'every monday 9am', "
    "'weekdays at 9am', 'every day at 21:30'; (4) cron syntax: '0 9 * * *' (daily at 9am); (5) one-shot "
    "timestamp: '2026-11-02T09:00' (phone-local time). Times are the phone's local time."
)


def _need(args: dict[str, Any], key: str, action: str) -> Any:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise tools.ToolError(f"cron {action} needs '{key}'")
    return value


def _flag(args: dict[str, Any], key: str) -> bool | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise tools.ToolError(f"'{key}' must be true or false")
    return value


def _repeat(args: dict[str, Any]) -> int | None:
    value = args.get("repeat")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise tools.ToolError("'repeat' must be a whole number (0 means forever)")
    return value


def _text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise tools.ToolError(f"'{key}' must be text")
    return value


def _check(args: dict[str, Any], action: str) -> None:
    """Everything that can be refused is refused before the owner is asked."""
    sched = _text(args, "schedule")
    prompt = _text(args, "prompt")
    if prompt is not None and (blocked := cron_scan.scan(prompt)):
        raise tools.ToolError(blocked)
    if sched is not None:
        try:
            schedule.parse(sched)
        except schedule.ScheduleError as e:
            raise tools.ToolError(f"{e}. {SCHEDULE_HELP}") from None
    if action == "create":
        _need(args, "schedule", action)
        _need(args, "prompt", action)


def _request(action: str, args: dict[str, Any], job: dict[str, Any] | None) -> approval.Request:
    shown = (_text(args, "prompt") or (job["prompt"] if job else "")).strip()
    if len(shown) > PROMPT_SHOWN:
        shown = shown[:PROMPT_SHOWN] + " ..."
    lines = []
    if job:
        lines.append(f"Job: {job['name']} ({job['id']})")
    if _text(args, "schedule"):
        lines.append(f"Schedule: {_text(args, 'schedule')}")
    elif job and job["schedule"]:
        lines.append(f"Schedule: {job['schedule'].get('display', '?')}")
    if action in ("create", "update") and shown:
        lines.append("Prompt, which runs unattended and messages you:\n" + shown)
    titles = {"create": "Schedule a new job", "update": "Change a scheduled job", "resume": "Resume a scheduled job",
              "remove": "Delete a scheduled job", "run": "Run a scheduled job now"}
    purpose = args.get("purpose") if isinstance(args.get("purpose"), str) else ""
    return approval.Request(f"cron {action}", title=titles[action], purpose=purpose, detail="\n".join(lines),
                            choices=(approval.ONCE, approval.DENY))


def _line(job: dict[str, Any]) -> str:
    return cron.describe_job(job)


def handle(s: Any, args: dict[str, Any], ctx: Any = None) -> str:
    store = getattr(ctx, "store", None)
    if store is None:
        raise tools.ToolError("scheduled jobs aren't available here (no database)")
    if getattr(ctx, "guest", False) or getattr(ctx, "actor", "foreground") != "foreground":
        return "denied: only the owner, in a conversation with them, can manage scheduled jobs"
    action = args.get("action")
    if action not in ACTIONS:
        raise tools.ToolError(f"action must be one of: {', '.join(ACTIONS)}")
    try:
        if action == "list":
            jobs = store.cron_jobs()
            if not jobs:
                return "There are no scheduled jobs."
            return f"{len(jobs)} job(s):\n" + "\n".join(_line(j) for j in jobs)
        ref = _need(args, "job_id", action) if action != "create" else None
        job = cron.resolve(store, ref) if ref else None
        _check(args, action)
        if action in GATED and getattr(ctx, "approve", None):
            choice = approval.decision(ctx.approve(_request(action, args, job)))
            if choice == approval.DENY:
                return "denied: the owner did not approve this. Don't retry it unless they ask."
        if action == "create":
            made = cron.create(store, _need(args, "schedule", action), _need(args, "prompt", action),
                               name=_text(args, "name"), repeat=_repeat(args), catch_up=_flag(args, "catch_up") is not False)
            return "Scheduled.\n" + _line(made) + _delivery_note()
        if action == "update":
            changed = cron.update(store, ref, schedule_text=_text(args, "schedule"), prompt=_text(args, "prompt"),
                                  name=_text(args, "name"), repeat=_repeat(args), catch_up=_flag(args, "catch_up"))
            return "Updated.\n" + _line(changed)
        if action == "pause":
            return "Paused.\n" + _line(cron.pause(store, ref))
        if action == "resume":
            return "Resumed.\n" + _line(cron.resume(store, ref))
        if action == "remove":
            gone = cron.remove(store, ref)
            return f"Removed job {gone['id']} ({gone['name']})."
        queued = cron.trigger(store, ref)
        return (f"Queued: {queued['name']} runs within a minute or so and messages the owner when it is done; "
                "its result won't appear in this conversation. Don't wait for it or poll.")
    except (cron.CronError, schedule.ScheduleError) as e:
        raise tools.ToolError(str(e)) from None


def _delivery_note() -> str:
    return ("\nA job runs only while the Telegram bridge is running, and its reply goes to the owner on Telegram. "
            "Each run is a fresh conversation that can't see this one, and it can use a risky tool (shell, "
            "files) only if the owner already allowed that exact action with 'Always'.")


tools.register(tools.Tool(
    "cron",
    "Schedule recurring or one-off jobs that run Chelus later on its own and message the owner the result, and "
    "manage them. WHEN: the owner asks for a reminder, a regular check or report, or something done at a "
    "set time. action 'create' needs schedule and prompt; 'list' shows the jobs; 'update', 'pause', 'resume', "
    "'remove' and 'run' take a job_id, so list first and never guess one. 'run' starts a job now in the "
    "background; its result goes to the owner, not to this conversation. A job runs in a fresh conversation "
    "with no memory of this chat, so its prompt must say everything it needs; its final reply is the message "
    "the owner gets, and it cannot ask questions. A job whose final reply is exactly [SILENT] sends nothing, "
    "so for 'only tell me if ...' tasks end the prompt with 'otherwise reply exactly [SILENT]'. Prefer "
    "updating an existing job over creating a near-duplicate. The owner is asked before anything is created, changed, resumed, run or removed. "
    + SCHEDULE_HELP,
    tools.obj({
        "action": {"type": "string", "enum": list(ACTIONS)},
        "job_id": {"type": "string", "description": "update, pause, resume, remove, run: the job's id (or its exact name), from list."},
        "schedule": {"type": "string", "description": "create (required) and update: when to run. " + SCHEDULE_HELP},
        "prompt": {"type": "string", "description": "create (required) and update: the complete, self-contained instructions the job follows."},
        "name": {"type": "string", "description": "Optional short name for the job."},
        "repeat": {"type": "integer", "description": "Optional: stop after this many runs. Omit or 0 for forever (a one-shot runs once)."},
        "catch_up": {"type": "boolean", "description": "Default true: if Chelus was off when a run was due, run once when it is back. False skips missed runs."},
        "purpose": tools.PURPOSE,
    }, ["action"]),
    handle, group="cron", max_output=6000))
