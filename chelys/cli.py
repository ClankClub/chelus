"""Command line: interactive chat, one-shot ask, and history browsing."""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__, provider
from .agent import Agent
from .config import ConfigError, Settings, load_api_key, load_settings
from .store import Store

EXIT_OK, EXIT_PROVIDER, EXIT_CONFIG, EXIT_INTERRUPT = 0, 1, 2, 130

CHAT_HELP = "Commands: /new starts a fresh conversation, /quit exits. Ctrl-C cancels the current reply."


def _event(kind: str, text: str) -> None:
    label = {"tool": "  > ", "result": "    = ", "denied": "  x denied: "}.get(kind, "  ")
    print(f"{label}{text}", file=sys.stderr, flush=True)


def _ask_permission(summary: str) -> bool:
    try:
        answer = input(f"  ? allow {summary} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _pick_session(store: Store, new: bool, session: int | None) -> int:
    if session is not None:
        if not store.session_exists(session):
            raise ConfigError(f"no conversation #{session} (see: chelys sessions)")
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
        print(f"Chelys {__version__} | {s.model} | conversation #{sid}"
              f"{f' (resumed, {past} messages)' if past else ' (new)'}")
        print(CHAT_HELP)
        agent = Agent(s, key, store, sid, _ask_permission, _event)
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
            try:
                reply = agent.run_turn(line)
            except KeyboardInterrupt:
                print("\n[cancelled]")
                continue
            except provider.ProviderError as e:
                print(f"[error] {e}")
                continue
            print(f"bot> {reply}")
    finally:
        store.close()


def cmd_ask(s: Settings, args: argparse.Namespace) -> int:
    key = load_api_key(s)
    store = Store(s.db_path)
    try:
        sid = _pick_session(store, args.new, args.session)

        def approve(summary: str) -> bool:
            return bool(args.allow_risky)

        agent = Agent(s, key, store, sid, approve, _event)
        try:
            reply = agent.run_turn(" ".join(args.text))
        except provider.ProviderError as e:
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


def cmd_telegram(s: Settings, args: argparse.Namespace) -> int:
    from . import telegram  # optional feature: only imported when used
    return telegram.run(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chelys", description="Chelys: a small agent that lives on this phone.")
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
    sub.add_parser("sessions", help="list conversations")
    sh = sub.add_parser("show", help="print a conversation (default: latest)")
    sh.add_argument("session", type=int, nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args(["chat", *(argv if argv is not None else sys.argv[1:])])
    handlers = {"chat": cmd_chat, "ask": cmd_ask, "sessions": cmd_sessions, "show": cmd_show,
                "telegram": cmd_telegram}
    try:
        s = load_settings()
        return handlers[args.command](s, args)
    except ConfigError as e:
        print(f"[config] {e}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\n[cancelled]", file=sys.stderr)
        return EXIT_INTERRUPT
