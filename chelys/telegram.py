"""Telegram bridge: chat with Chelys from a Telegram bot.

Long polling over HTTPS with the standard library, so the phone needs no public address
or open port. Only the owner's user ID, in a private chat, is answered; every other
update is dropped unread. Risky tools ask for approval with Yes/No buttons.

Hermes Agent's Telegram adapter is built on python-telegram-bot, httpx and asyncio, so
it can't run here. This file copies its behavior, not its code.
"""

from __future__ import annotations

import json
import secrets
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from . import provider
from .agent import Agent
from .config import ConfigError, Settings, load_api_key
from .store import SessionBusy, Store

API = "https://api.telegram.org"
MAX_TEXT = 4000            # Telegram's limit is 4096 characters; leave room
POLL_SECONDS = 50          # long-poll wait on Telegram's side
APPROVAL_SECONDS = 180     # unanswered approval requests count as "no"

HELP = (
    "Chelys, on the phone.\n"
    "Just talk to me. Commands:\n"
    "/new  start a fresh conversation\n"
    "/stop  cancel an automatic retry while DeepSeek is down\n"
    "/help  this message"
)


class TelegramError(Exception):
    """A failed Telegram call. The message never contains the bot token."""


def load_telegram_config(s: Settings) -> tuple[str, int]:
    token_path = s.config_dir / "telegram.token"
    id_path = s.config_dir / "telegram_user_id"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(f"no Telegram bot token. Put it in {token_path} (chmod 600).") from None
    try:
        raw_id = id_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(f"no owner user ID. Put your numeric Telegram user ID in {id_path}.") from None
    if not token or ":" not in token:
        raise ConfigError(f"{token_path} doesn't look like a bot token")
    if not raw_id.isdigit():
        raise ConfigError(f"{id_path} must contain only your numeric Telegram user ID")
    return token, int(raw_id)


def load_guests(s: Settings, owner: int) -> set[int]:
    """Optional telegram_guests file: one numeric user ID per line, # comments allowed.
    Guests can chat and use safe tools in their own conversation; risky tools are refused."""
    path = s.config_dir / "telegram_guests"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    guests = set()
    for n, line in enumerate(lines, 1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if not entry.isdigit():
            raise ConfigError(f"{path} line {n}: expected a numeric Telegram user ID, got {entry!r}")
        guests.add(int(entry))
    guests.discard(owner)
    return guests


class Bot:
    """Minimal Telegram Bot API client."""

    def __init__(self, token: str, timeout_pad: float = 15.0, api: str = API):
        self._token = token
        self._api = api
        self._pad = timeout_pad
        self._ctx = ssl.create_default_context()

    def call(self, method: str, params: dict[str, Any] | None = None, wait: float = 0) -> Any:
        body = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._api}/bot{self._token}/{method}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=wait + self._pad, context=self._ctx) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("description", "")
            except Exception:
                detail = ""
            finally:
                e.close()
            raise TelegramError(f"Telegram {method} returned HTTP {e.code} {detail}".strip()) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise TelegramError(f"Telegram {method} failed: {type(reason).__name__}: {reason}".replace(self._token, "<token>")) from None
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method}: {data.get('description', 'not ok')}")
        return data["result"]

    def send(self, chat_id: int, text: str, **extra: Any) -> None:
        for chunk in split_text(text):
            self.call("sendMessage", {"chat_id": chat_id, "text": chunk, **extra})


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line breaks where possible, so long replies arrive as readable pieces."""
    text = text or "(empty)"
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class Bridge:
    def __init__(self, s: Settings, bot: Bot, owner_id: int, agent_factory, log=print,
                 guests: set[int] | None = None):
        self.s = s
        self.bot = bot
        self.owner = owner_id
        self.guests = set(guests or ())
        self.make_agent = agent_factory
        self.log = log
        self.offset = 0
        self.pending: list[dict[str, Any]] = []   # owner messages that arrived during an approval wait
        self.ignored = 0

    # ---- update intake -------------------------------------------------

    def _fetch(self, wait: float) -> list[dict[str, Any]]:
        updates = self.bot.call(
            "getUpdates",
            {"offset": self.offset, "timeout": int(wait), "allowed_updates": ["message", "callback_query"]},
            wait=wait,
        )
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        return updates

    def _is_owner_message(self, u: dict[str, Any]) -> bool:
        m = u.get("message")
        if not m:
            return False
        sender = (m.get("from") or {}).get("id")
        ok = (sender == self.owner or sender in self.guests) and (m.get("chat") or {}).get("type") == "private"
        if not ok:
            self.ignored += 1
        return ok

    def _is_owner_callback(self, u: dict[str, Any]) -> bool:
        cq = u.get("callback_query")
        if not cq:
            return False
        if (cq.get("from") or {}).get("id") != self.owner:
            self.ignored += 1
            return False
        return True

    # ---- approval through buttons --------------------------------------

    def approve(self, chat_id: int, summary: str) -> bool:
        nonce = secrets.token_hex(4)
        self.bot.call("sendMessage", {
            "chat_id": chat_id,
            "text": f"Allow this?\n{summary}",
            "reply_markup": {"inline_keyboard": [[
                {"text": "Yes", "callback_data": f"ok:{nonce}"},
                {"text": "No", "callback_data": f"no:{nonce}"},
            ]]},
        })
        deadline = time.monotonic() + APPROVAL_SECONDS
        while time.monotonic() < deadline:
            wait = max(1, min(POLL_SECONDS, deadline - time.monotonic()))
            for u in self._fetch(wait):
                if self._is_owner_callback(u):
                    cq = u["callback_query"]
                    data = cq.get("data", "")
                    verdict, _, got = data.partition(":")
                    if got != nonce:
                        self._answer_callback(cq, "That request has expired.")
                        continue
                    allowed = verdict == "ok"
                    self._answer_callback(cq, "Approved" if allowed else "Denied")
                    self._close_buttons(cq, f"{'Approved' if allowed else 'Denied'}: {summary}")
                    return allowed
                if self._is_owner_message(u):
                    self.pending.append(u["message"])
        self.bot.send(chat_id, "No answer in time, so that action was denied.")
        return False

    def _answer_callback(self, cq: dict[str, Any], text: str) -> None:
        try:
            self.bot.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text})
        except TelegramError:
            pass

    def _close_buttons(self, cq: dict[str, Any], text: str) -> None:
        msg = cq.get("message") or {}
        try:
            self.bot.call("editMessageText", {
                "chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "text": text,
            })
        except (TelegramError, KeyError):
            pass

    # ---- waiting out an outage ------------------------------------------

    def wait_out(self, chat_id: int, user: int, seconds: float, cycle: int, total: int, error: str) -> bool:
        """One auto-recovery wait with a countdown in the chat. Keeps polling Telegram so
        /stop from the person whose turn it is cancels it; other messages wait their turn.
        Returns False if stopped."""
        self.bot.send(chat_id, f"[waiting] DeepSeek is unavailable ({error}). Retrying automatically in "
                               f"{seconds:.0f}s (try {cycle}/{total}). Send /stop to cancel.")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            wait = max(1, min(POLL_SECONDS, int(deadline - time.monotonic())))
            for u in self._fetch(wait):
                if self._is_owner_message(u):
                    m = u["message"]
                    if (m.get("text") or "").strip().split("@")[0].lower() == "/stop" and m["from"]["id"] == user:
                        self.bot.send(chat_id, "Stopped retrying.")
                        return False
                    self.pending.append(m)
                elif self._is_owner_callback(u):
                    self._answer_callback(u["callback_query"], "That request has expired.")
        return True

    # ---- handling owner messages ---------------------------------------

    def handle(self, m: dict[str, Any]) -> None:
        chat_id = m["chat"]["id"]
        user = m["from"]["id"]
        is_owner = user == self.owner
        text = (m.get("text") or "").strip()
        if not text:
            self.bot.send(chat_id, "I can only read text for now.")
            return
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/help"):
            self.bot.send(chat_id, HELP)
            return
        if command == "/new":
            sid = self.make_agent.new_session(user)
            self.bot.send(chat_id, f"Started conversation #{sid}.")
            return
        if command == "/stop":
            self.bot.send(chat_id, "Nothing to stop right now.")
            return

        def on_event(kind: str, detail: str) -> None:
            if kind in ("retry", "wait", "guard", "notice"):
                self.log(f"[telegram] {kind}: {detail}")
            if kind == "wait":  # a long retry wait: say so instead of going quiet for minutes
                try:
                    self.bot.send(chat_id, f"[waiting] {detail}")
                except TelegramError:
                    pass

        if is_owner:
            def approve(summary: str) -> bool:
                return self.approve(chat_id, summary)
        else:
            def approve(summary: str) -> bool:
                self.log(f"[telegram] refused risky tool for guest {user}: {summary}")
                return False  # guests never get risky tools

        def autorecover(seconds: float, cycle: int, total: int, error: str) -> bool:
            return self.wait_out(chat_id, user, seconds, cycle, total, error)

        agent = self.make_agent(user, approve, on_event, autorecover=autorecover)
        try:
            self.bot.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass
        try:
            reply = agent.run_turn(text)
        except (provider.ProviderError, SessionBusy) as e:
            self.bot.send(chat_id, f"[error] {e}")
            return
        if agent.tools_ok:  # only tools that actually ran, not denied or failed calls
            reply = f"[used {', '.join(dict.fromkeys(agent.tools_ok))}]\n{reply}"
        self.bot.send(chat_id, reply)

    def run_forever(self) -> None:
        me = self.bot.call("getMe")
        who = f"owner {self.owner}" + (f" and {len(self.guests)} guest(s)" if self.guests else "")
        self.log(f"Telegram bridge running as @{me.get('username')}; answering only {who}. Ctrl-C stops it.")
        backoff = 5
        while True:
            try:
                while self.pending:
                    self.handle(self.pending.pop(0))
                for u in self._fetch(POLL_SECONDS):
                    if self._is_owner_message(u):
                        self.handle(u["message"])
                    elif self._is_owner_callback(u):
                        self._answer_callback(u["callback_query"], "That request has expired.")
                backoff = 5
            except TelegramError as e:
                self.log(f"[telegram] {e}; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:  # a bug handling one message must not kill the bridge
                self.log(f"[telegram] unexpected {type(e).__name__}: {e}")
                time.sleep(1)


class AgentFactory:
    """Builds an Agent per message. Each Telegram user has their own conversation,
    remembered across restarts in a small file per user."""

    def __init__(self, s: Settings, api_key: str, store: Store, owner: int):
        self.s = s
        self.api_key = api_key
        self.store = store
        self.owner = owner

    def _session_file(self, user: int):
        name = "telegram_session" if user == self.owner else f"telegram_session_{user}"
        return self.s.data_dir / name

    def session_for(self, user: int) -> int:
        try:
            sid = int(self._session_file(user).read_text().strip())
            if self.store.session_exists(sid):
                return sid
        except (FileNotFoundError, ValueError):
            pass
        return self.new_session(user)

    def new_session(self, user: int) -> int:
        sid = self.store.new_session()
        path = self._session_file(user)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(sid))
        return sid

    def __call__(self, user: int, approve, on_event, autorecover=None) -> Agent:
        # Unattended surface: the loop guard may stop a turn, and outages are waited out
        # (Hermes does both on its gateway).
        return Agent(self.s, self.api_key, self.store, self.session_for(user), approve, on_event,
                     hard_stop=True, autorecover=autorecover)


def run(s: Settings) -> int:
    token, owner = load_telegram_config(s)
    guests = load_guests(s, owner)
    api_key = load_api_key(s)
    store = Store(s.db_path)
    try:
        def log(line: str) -> None:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", flush=True)

        bridge = Bridge(s, Bot(token), owner, AgentFactory(s, api_key, store, owner), log=log, guests=guests)
        bridge.run_forever()
    finally:
        store.close()
    return 0
