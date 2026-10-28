"""Command line: interactive chat, one-shot ask, and history browsing."""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__, approval, curator, learn, memory, provider, review, skill_tools, skills
from .agent import LEARN_STEPS, Agent
from .config import ConfigError, Settings, load_api_key, load_settings
from .store import SessionBusy, Store

EXIT_OK, EXIT_PROVIDER, EXIT_CONFIG, EXIT_INTERRUPT = 0, 1, 2, 130

CHAT_HELP = ("Commands: /new starts a fresh conversation, /learn [what] turns something into a skill, "
             "/memory shows what Chelus remembers, /approvals [revoke ID|clear] manages tool grants, "
             "/quit exits. Ctrl-C cancels the current reply. "
             "More: chelus skills --help, chelus memory --help")
CHOICE_KEYS = {"o": approval.ONCE, "y": approval.ONCE, "yes": approval.ONCE, "s": approval.SESSION,
               "a": approval.ALWAYS}


def _event(kind: str, text: str) -> None:
    label = {"tool": "  > ", "result": "    = ", "denied": "  x denied: ", "retry": "  ~ ", "wait": "  ~ ",
             "guard": "  ! ", "notice": "  ~ ", "usage": "  . ", "memory": "  + remembered: "}.get(kind, "  ")
    print(f"{label}{text}", file=sys.stderr, flush=True)


def _ask_permission(req: str) -> str:
    if not isinstance(req, approval.Request):
        req = approval.Request(req)
    print("\n  " + req.card().replace("\n", "\n  "))
    menu = {approval.ONCE: "[o]nce", approval.SESSION: "[s]ession", approval.ALWAYS: "[a] " + req.label(approval.ALWAYS),
            approval.DENY: "[N]o"}
    try:
        answer = input("  ? " + "  ".join(menu[c] for c in req.choices) + " ").strip().lower()
    except EOFError:
        return approval.DENY
    choice = CHOICE_KEYS.get(answer, approval.DENY)
    return choice if choice in req.choices else approval.DENY


def _wait_out(seconds: float, cycle: int, total: int, error: str) -> bool:
    """Interactive chat waits out an outage too; Ctrl-C cancels the wait and the turn."""
    _event("wait", f"DeepSeek is unavailable ({error}); retrying automatically in {seconds:.0f}s "
                   f"(try {cycle}/{total}); Ctrl-C to cancel")
    time.sleep(seconds)
    return True


def _pick_session(store: Store, new: bool, session: int | None) -> int:
    if session is not None:
        if not store.session_exists(session):
            raise ConfigError(f"no conversation #{session} (see: chelus sessions)")
        return session
    if new:
        return store.new_session()
    return store.latest_session() or store.new_session()


def cmd_chat(s: Settings, args: argparse.Namespace) -> int:
    key = load_api_key(s)
    store = Store(s.db_path)
    try:
        sid = _pick_session(store, args.new, args.session)
        past = len(store.messages(sid))
        print(f"Chelus {__version__} | {s.model} | conversation #{sid}"
              f"{f' (resumed, {past} messages)' if past else ' (new)'}")
        print(CHAT_HELP)
        agent = Agent(s, key, store, sid, _ask_permission, _event, autorecover=_wait_out)
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return EXIT_OK
            if not line:
                continue
            if line in ("/quit", "/exit"):
                return EXIT_OK
            if line == "/new":
                agent.session_id = store.new_session()
                print(f"started conversation #{agent.session_id}")
                continue
            if line == "/help":
                print(CHAT_HELP)
                continue
            if line == "/approvals" or line.startswith("/approvals "):
                print(approval.manage(store, line[len("/approvals"):]))
                continue
            if line == "/memory":
                print(memory.owner_view(s))
                continue
            api_text, steps = None, None
            if line == "/learn" or line.startswith("/learn "):
                api_text, steps = learn.build_prompt(line[len("/learn"):]), LEARN_STEPS
            try:
                reply = agent.run_turn(line, api_text=api_text, step_limit=steps)
            except KeyboardInterrupt:
                print("\n[cancelled]")
                continue
            except (provider.ProviderError, SessionBusy) as e:
                print(f"[error] {e}")
                continue
            print(f"bot> {reply}")
            if agent.review_due or agent.memory_review_due:
                try:
                    result = review.run(agent)
                except KeyboardInterrupt:
                    print("\n[review cancelled]")
                    continue
                if result.staged:
                    print(f"  ~ {len(result.staged)} change(s) wait for you: chelus skills pending")
    finally:
        store.close()


def cmd_ask(s: Settings, args: argparse.Namespace) -> int:
    key = load_api_key(s)
    store = Store(s.db_path)
    try:
        sid = _pick_session(store, args.new, args.session)

        def approve(summary: str) -> bool:
            return bool(args.allow_risky)

        agent = Agent(s, key, store, sid, approve, _event, reusable_approvals=False)
        try:
            reply = agent.run_turn(" ".join(args.text))
        except (provider.ProviderError, SessionBusy) as e:
            print(f"[error] {e}", file=sys.stderr)
            return EXIT_PROVIDER
        print(reply)
        return EXIT_OK
    finally:
        store.close()


def cmd_sessions(s: Settings, args: argparse.Namespace) -> int:
    store = Store(s.db_path)
    try:
        rows = store.list_sessions()
        if not rows:
            print("no conversations yet")
        for sid, updated, title, count in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(updated))
            print(f"#{sid:<4} {when}  {count:>3} msgs  {title}")
        return EXIT_OK
    finally:
        store.close()


def cmd_show(s: Settings, args: argparse.Namespace) -> int:
    store = Store(s.db_path)
    try:
        sid = args.session if args.session is not None else store.latest_session()
        if sid is None or not store.session_exists(sid):
            print("no such conversation")
            return EXIT_CONFIG
        archived = store.archived_count(sid)
        if archived:
            print(f"[{archived} older messages were summarized; the summary rides with the first message below]")
        for m in store.messages(sid):
            role = m.get("role")
            if role == "tool":
                print(f"  = {(m.get('content') or '').splitlines()[0][:120] if m.get('content') else ''}")
            elif m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    print(f"  > {tc['function']['name']} {tc['function']['arguments'][:120]}")
            else:
                print(f"{'you' if role == 'user' else 'bot'}> {m.get('content')}")
        return EXIT_OK
    finally:
        store.close()


def cmd_usage(s: Settings, args: argparse.Namespace) -> int:
    store = Store(s.db_path)
    try:
        sid = args.session if args.session is not None else store.latest_session()
        rows = [(f"conversation #{sid}", store.usage_totals(sid))] if sid is not None else []
        rows.append(("all conversations", store.usage_totals()))
        for label, t in rows:
            hit_rate = f"{100 * t['cache_hit'] / t['prompt']:.0f}%" if t["prompt"] else "-"
            print(f"{label}: {t['calls']} calls, {t['prompt']} tokens in ({t['cache_hit']} cached, "
                  f"{hit_rate}), {t['completion']} out")
        return EXIT_OK
    finally:
        store.close()


def cmd_skills(s: Settings, args: argparse.Namespace) -> int:
    store = Store(s.db_path)
    base = skills.root(s)
    try:
        act, name = args.action, args.name
        if act in ("view", "approve", "reject", "undo", "pin", "unpin", "revoke") and not name:
            print(f"usage: chelus skills {act} NAME", file=sys.stderr)
            return EXIT_CONFIG
        if act == "list":
            found = skills.discover(base)
            usage = {r["name"]: r for r in store.usage_rows()}
            for k in found:
                u = usage.get(k.name) or {}
                tags = [t for t in (u.get("created_by"), u.get("state") if u.get("state") != "active" else None,
                                    "pinned" if u.get("pinned") else None) if t]
                print(f"{k.category or 'general'}/{k.name}: {k.description}" + (f"  [{', '.join(tags)}]" if tags else ""))
            if not found:
                print(f"no skills yet (they live in {base})")
            for p in skills.archived(base):
                print(f"archived: {p.name}")
        elif act == "view":
            print(skills.view(base, name))
        elif act == "pending":
            recs = store.pending()
            if not recs:
                print("no skill changes are waiting")
            for rec in recs:
                print(skill_tools.pending_card(rec, s) + "\n")
        elif act == "approve":
            print(skill_tools.apply_pending(s, store, name))
        elif act == "reject":
            print(skill_tools.reject_pending(store, name))
        elif act == "undo":
            print(skill_tools.undo(s, store, name))
        elif act in ("pin", "unpin"):
            store.set_usage(name, pinned=1 if act == "pin" else 0)
            print(f"{name}: {'pinned (the curator leaves it alone)' if act == 'pin' else 'unpinned'}")
        elif act == "revoke":
            print(f"{name}: no longer always allowed" if store.revoke(name) else f"{name} had no standing approval")
        elif act == "history":
            for rid, ts, skill, file, actor, action, undone in store.revisions(name):
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                print(f"{when}  {skill:<20} {action:<11} {file or '-':<24} by {actor}{'  (undone)' if undone else ''}")
        elif act == "curate":
            print(curator.run(s, store))
        return EXIT_OK
    except skills.SkillError as e:
        print(f"[skills] {e}", file=sys.stderr)
        return EXIT_CONFIG
    finally:
        store.close()


def cmd_memory(s: Settings, args: argparse.Namespace) -> int:
    if args.action == "show":
        print(memory.owner_view(s))
        return EXIT_OK
    if args.action == "forget":
        if len(args.rest) != 2:
            print("usage: chelus memory forget user|memory N", file=sys.stderr)
            return EXIT_CONFIG
        print(memory.forget(s, *args.rest))
        return EXIT_OK
    print(memory.root(s))
    return EXIT_OK


def cmd_approvals(s: Settings, args: argparse.Namespace) -> int:
    store = Store(s.db_path)
    try:
        print(approval.manage(store, " ".join(args.rest)))
        return EXIT_OK
    finally:
        store.close()


def cmd_cron(s: Settings, args: argparse.Namespace) -> int:
    """Scheduled jobs. The Telegram bridge runs them; `run` runs one here and prints it."""
    from . import cron, schedule
    store = Store(s.db_path)
    try:
        act = args.action
        if act == "list":
            jobs = store.cron_jobs()
            print("\n".join(cron.describe_job(j) for j in jobs) or "No scheduled jobs.")
            if jobs:
                print("\nJobs run while the Telegram bridge is running (chelus telegram).")
        elif act == "add":
            if len(args.rest) < 2:
                raise cron.CronError('usage: chelus cron add [--name N] [--repeat N] SCHEDULE PROMPT... '
                                     '(options first; quote the schedule: "every day at 9am")')
            job = cron.create(store, args.rest[0], " ".join(args.rest[1:]), name=args.name,
                              repeat=args.repeat, catch_up=not args.no_catch_up)
            print("Added:\n" + cron.describe_job(job))
        elif act in ("pause", "resume", "remove"):
            if len(args.rest) != 1:
                raise cron.CronError(f"usage: chelus cron {act} JOB")
            job = getattr(cron, act)(store, args.rest[0])
            print(("Removed: " if act == "remove" else "") + cron.describe_job(job))
        elif act == "runs":
            job_id = cron.resolve(store, args.rest[0])["id"] if args.rest else None
            for r in store.cron_runs(job_id):
                when = schedule.describe(r["finished"] or r["claimed"])
                print(f"#{r['id']} {r['job_id']} {r['kind']}: {r['status']} at {when}, delivery {r['delivery']}"
                      + (f", conversation #{r['session_id']}" if r["session_id"] else "")
                      + (f"\n    {r['error']}" if r["error"] else ""))
        elif act == "run":
            if len(args.rest) != 1:
                raise cron.CronError("usage: chelus cron run JOB")
            runner = cron.Runner(s, load_api_key(s), store, log=lambda line: print(line, file=sys.stderr))
            res = cron.run_now(store, runner, args.rest[0])
            print(res.text or "(silent: the job had nothing to report)")
            return EXIT_OK if res.status in ("ok", "silent") else EXIT_PROVIDER
        return EXIT_OK
    except (cron.CronError, schedule.ScheduleError) as e:
        print(f"[cron] {e}", file=sys.stderr)
        return EXIT_CONFIG
    finally:
        store.close()


def cmd_telegram(s: Settings, args: argparse.Namespace) -> int:
    from . import telegram  # optional feature: only imported when used
    return telegram.run(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chelus", description="Chelus: a small agent that lives on this phone.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")

    def session_flags(sp: argparse.ArgumentParser) -> None:
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--new", action="store_true", help="start a new conversation")
        g.add_argument("--session", type=int, metavar="N", help="continue conversation N")

    c = sub.add_parser("chat", help="interactive chat (default; resumes the latest conversation)")
    session_flags(c)
    a = sub.add_parser("ask", help="one message, print the reply, exit")
    session_flags(a)
    a.add_argument("--allow-risky", action="store_true", help="let write_file and run_shell run without asking")
    a.add_argument("text", nargs="+")
    sub.add_parser("telegram", help="run the Telegram bot bridge (answers only the owner)")
    ap = sub.add_parser("approvals", help="list ordinary tool grants; revoke ID or clear")
    ap.add_argument("rest", nargs="*")
    sub.add_parser("sessions", help="list conversations")
    sh = sub.add_parser("show", help="print a conversation (default: latest)")
    sh.add_argument("session", type=int, nargs="?")
    u = sub.add_parser("usage", help="token usage and cache hits (default: latest conversation)")
    u.add_argument("session", type=int, nargs="?")
    sk = sub.add_parser("skills", help="list, review, undo and manage skills")
    sk.add_argument("action", nargs="?", default="list",
                    choices=["list", "view", "pending", "approve", "reject", "undo", "history", "pin", "unpin",
                             "revoke", "curate"],
                    help="list (default); view NAME; pending; approve ID; reject ID; undo NAME (the last change); "
                         "history [NAME]; pin/unpin NAME (curator); revoke NAME (drop 'always'); curate (run now)")
    sk.add_argument("name", nargs="?")
    cr = sub.add_parser("cron", help="scheduled jobs: list, add, pause, resume, remove, run, runs")
    cr.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "pause", "resume", "remove", "run", "runs"],
                    help="list (default); add [--name N] [--repeat N] SCHEDULE PROMPT... (options first); "
                         "pause/resume/remove/run JOB; runs [JOB]. "
                         "Schedules: '30m', 'every 2h', 'in 30m', 'every monday 9am', 'weekdays at 9am', "
                         "'0 9 * * *', '2026-11-02T09:00'")
    cr.add_argument("rest", nargs="*")
    cr.add_argument("--name", help="add: a short name (default: the prompt's first line)")
    cr.add_argument("--repeat", type=int, help="add: stop after this many runs (default: forever)")
    cr.add_argument("--no-catch-up", action="store_true",
                    help="add: skip a run missed by more than its grace window instead of running it late")
    me = sub.add_parser("memory", help="show what Chelus remembers, or forget an entry")
    me.add_argument("action", nargs="?", default="show", choices=["show", "forget", "path"],
                    help="show (default); forget user|memory N; path (where the files are, to edit by hand). "
                         "Changes the background review proposes are under: chelus skills pending")
    me.add_argument("rest", nargs="*")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args(["chat", *(argv if argv is not None else sys.argv[1:])])
    handlers = {"chat": cmd_chat, "ask": cmd_ask, "sessions": cmd_sessions, "show": cmd_show,
                "telegram": cmd_telegram, "usage": cmd_usage, "skills": cmd_skills, "memory": cmd_memory,
                "approvals": cmd_approvals, "cron": cmd_cron}
    try:
        s = load_settings()
        return handlers[args.command](s, args)
    except ConfigError as e:
        print(f"[config] {e}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\n[cancelled]", file=sys.stderr)
        return EXIT_INTERRUPT
