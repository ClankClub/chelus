"""Telegram bridge: chat with Chelus from a Telegram bot.

Long polling over HTTPS with the standard library, so the phone needs no public address
or open port. Only the owner's user ID, in a private chat, is answered; every other
update is dropped unread. Risky tools ask for approval with Yes/No buttons.

Hermes Agent's Telegram adapter is built on python-telegram-bot, httpx and asyncio, so
it can't run here. This file copies its behavior, not its code.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

from . import approval, cron, curator, learn, memory, provider, review, skill_tools, skills
from .agent import LEARN_STEPS, Agent
from .config import ConfigError, Settings, load_api_key
from .stats import RuntimeStats, StatsPreferences
from .store import SessionBusy, Store

API = "https://api.telegram.org"
MAX_TEXT = 4000            # Telegram's limit is 4096 characters; leave room
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # Bound Telegram download and base64 request size
POLL_SECONDS = 50          # long-poll wait on Telegram's side
APPROVAL_SECONDS = 180     # unanswered approval requests count as "no"
CARD_DETAIL = 2500         # longest diff or command shown on an approval card

HELP = (
    "Chelus, on the phone.\n"
    "Just talk to me. Commands:\n"
    "/new  start a fresh conversation\n"
    "/learn [what]  turn something into a skill (empty: what we just did)\n"
    "/skills  list skills and waiting changes\n"
    "/approvals [revoke ID|clear]  ordinary tool grants\n"
    "/pending  show skill changes waiting for your OK\n"
    "/undo <skill>  undo the last change to a skill\n"
    "/memory  what I remember about you and this phone\n"
    "/memory forget user|memory N  delete entry N\n"
    "/stop  cancel an automatic retry while DeepSeek is down\n"
    "/stats [on|off|info]  toggle/set runtime footer, or show telemetry\n"
    "/stats add|remove mem cpu bat swap up disk  select fields for this chat\n"
    "/stats reset  restore default mem/cpu/bat and enable footer\n"
    "/help  this message"
)
GUEST_HELP = (
    "Chelus, on the phone.\n"
    "Just talk to me. Commands:\n"
    "/new  start a fresh conversation\n"
    "/stats [on|off|info]  toggle/set runtime footer, or show telemetry\n"
    "/stats add|remove mem cpu bat swap up disk  select fields for this chat\n"
    "/stats reset  restore default mem/cpu/bat and enable footer\n"
    "/help  this message"
)
OWNER_ONLY = ("/learn", "/skills", "/pending", "/undo", "/memory", "/approvals")
# Approval buttons: callback code -> choice
CODES = {"ok": approval.ONCE, "se": approval.SESSION, "al": approval.ALWAYS, "no": approval.DENY}
CHOICE_CODES = {v: k for k, v in CODES.items()}
DONE_LABELS = {approval.ONCE: "Allowed once", approval.SESSION: "Allowed for this conversation",
               approval.ALWAYS: "Always allowed for this skill", approval.DENY: "Denied"}


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

    def photo_data_url(self, file_id: str) -> str:
        # Fetch a Telegram photo for one model request; never persist its bytes.
        if not isinstance(file_id, str) or not file_id:
            raise TelegramError("Telegram photo has no file ID")
        info = self.call("getFile", {"file_id": file_id})
        if not isinstance(info, dict):
            raise TelegramError("Telegram returned invalid photo metadata")
        file_path = info.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramError("Telegram returned no photo path")
        file_size = info.get("file_size")
        if isinstance(file_size, int) and file_size > MAX_IMAGE_BYTES:
            raise TelegramError("Photo exceeds the 10 MiB limit")
        file_url = f"{self._api}/file/bot{self._token}/{urllib.parse.quote(file_path, safe='/')}"
        req = urllib.request.Request(file_url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._pad, context=self._ctx) as resp:
                raw = resp.read(MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as e:
            e.close()
            raise TelegramError(f"Telegram photo download returned HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise TelegramError(f"Telegram photo download failed: {type(reason).__name__}") from None
        if len(raw) > MAX_IMAGE_BYTES:
            raise TelegramError("Photo exceeds the 10 MiB limit")
        if not raw:
            raise TelegramError("Telegram returned an empty photo")
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        if not mime.startswith("image/"):
            raise TelegramError("Telegram file is not an image")
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

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
        self.updates: deque[dict[str, Any]] = deque()
        self.pending: list[dict[str, Any]] = []   # owner messages that arrived during an approval wait
        self.callbacks: list[dict[str, Any]] = [] # pending-skill buttons pressed during a wait
        self.ignored = 0
        self.last_activity = time.monotonic()
        self.stats = RuntimeStats(s.workdir)
        self.stats_preferences = StatsPreferences(s.data_dir)
        self.cron: cron.Runner | None = None  # scheduled jobs; set by run()

    def _decorate(self, chat_id: int, text: str) -> str:
        footer = self.stats.footer(self.stats_preferences.fields(chat_id)) if self.stats_preferences.enabled(chat_id) else ""
        return text + "\n\n" + footer if footer else text

    def _payloads(self, chat_id: int, text: str, decorate: bool = True) -> list[str]:
        """Reserve footer space on every chunk; preserve all body characters.

        Count UTF-16 units conservatively so astral characters also fit Telegram.
        """
        suffix = self._decorate(chat_id, "") if decorate else ""
        limit = MAX_TEXT - len(suffix)
        chunks = []
        start = units = 0
        for i, char in enumerate(text or "(empty)"):
            width = 2 if ord(char) > 0xffff else 1
            if units + width > limit:
                chunks.append(text[start:i] + suffix)
                start, units = i, 0
            units += width
        chunks.append((text or "(empty)")[start:] + suffix)
        return chunks

    def _send(self, chat_id: int, text: str, *, decorate: bool = True, **extra: Any) -> int | None:
        """Send text in as many pieces as it needs. Returns the last piece's message id, or None
        if Telegram didn't return one."""
        message_id = None
        for chunk in self._payloads(chat_id, text, decorate=decorate):
            sent = self.bot.call("sendMessage", {"chat_id": chat_id, "text": chunk, **extra})
            message_id = sent.get("message_id") if isinstance(sent, dict) else None
        return message_id

    # ---- update intake -------------------------------------------------

    def _fetch(self, wait: float) -> list[dict[str, Any]]:
        # Deliver one event at a time. Nested approval/outage polls and early returns
        # must leave the remainder of a fetched batch available to the next consumer.
        if not self.updates:
            updates = self.bot.call(
                "getUpdates",
                {"offset": self.offset, "timeout": int(wait), "allowed_updates": ["message", "callback_query"]},
                wait=wait,
            )
            self.updates.extend(updates)
            if updates:
                self.offset = updates[-1]["update_id"] + 1
        return [self.updates.popleft()] if self.updates else []

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

    def approve(self, chat_id: int, req: str) -> str:
        """Ask the owner with buttons; returns a choice (approval.ONCE ... DENY)."""
        if not isinstance(req, approval.Request):
            req = approval.Request(req)
        nonce = secrets.token_hex(4)
        buttons = [{"text": req.label(c), "callback_data": f"{CHOICE_CODES[c]}:{nonce}"}
                   for c in req.choices]
        card = req.card()
        if len(card) > CARD_DETAIL + 400:
            card = card[:CARD_DETAIL + 400] + "\n... (cut off)"
        self._send(chat_id, f"Allow this?\n{card}\n\n(No answer in {APPROVAL_SECONDS // 60} min counts as Deny.)",
                   reply_markup={"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]})
        deadline = time.monotonic() + APPROVAL_SECONDS
        while time.monotonic() < deadline:
            wait = max(1, min(POLL_SECONDS, deadline - time.monotonic()))
            for u in self._fetch(wait):
                if self._is_owner_callback(u):
                    cq = u["callback_query"]
                    data = cq.get("data", "")
                    if data.startswith("sk:"):
                        self.callbacks.append(cq)  # a pending-skill button: handled after this turn
                        continue
                    code, _, got = data.partition(":")
                    if got != nonce or code not in CODES or CODES[code] not in req.choices:
                        self._answer_callback(cq, "That request has expired.")
                        continue
                    choice = CODES[code]
                    done = ("Always allowed for this action" if choice == approval.ALWAYS and not req.skill
                            else DONE_LABELS[choice])
                    self._answer_callback(cq, done)
                    fact = req.detail.split("\n", 1)[0] if req.detail else ""
                    self._close_buttons(cq, f"{done}: {req.title}" + (f"\n{fact}" if fact else ""))
                    return choice
                if self._is_owner_message(u):
                    self.pending.append(u["message"])
        self._send(chat_id, "No answer in time, so that action was denied.")
        return approval.DENY

    def _answer_callback(self, cq: dict[str, Any], text: str) -> None:
        try:
            self.bot.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text})
        except TelegramError:
            pass

    def _close_buttons(self, cq: dict[str, Any], text: str) -> None:
        msg = cq.get("message") or {}
        try:
            chat_id = msg["chat"]["id"]
            chunks = self._payloads(chat_id, text)
            self.bot.call("editMessageText", {
                "chat_id": chat_id, "message_id": msg["message_id"], "text": chunks[0],
            })
            for chunk in chunks[1:]:
                self.bot.call("sendMessage", {"chat_id": chat_id, "text": chunk})
        except (TelegramError, KeyError):
            pass

    # ---- waiting out an outage ------------------------------------------

    def wait_out(self, chat_id: int, user: int, seconds: float, cycle: int, total: int, error: str) -> bool:
        """One auto-recovery wait with a countdown in the chat. Keeps polling Telegram so
        /stop from the person whose turn it is cancels it; other messages wait their turn.
        Returns False if stopped."""
        self._send(chat_id, f"[waiting] DeepSeek is unavailable ({error}). Retrying automatically in "
                               f"{seconds:.0f}s (try {cycle}/{total}). Send /stop to cancel.")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            wait = max(1, min(POLL_SECONDS, int(deadline - time.monotonic())))
            for u in self._fetch(wait):
                if self._is_owner_message(u):
                    m = u["message"]
                    if (m.get("text") or "").strip().split("@")[0].lower() == "/stop" and m["from"]["id"] == user:
                        self._send(chat_id, "Stopped retrying.")
                        return False
                    self.pending.append(m)
                elif self._is_owner_callback(u):
                    self._expired_or_stash(u["callback_query"])
        return True

    def _expired_or_stash(self, cq: dict[str, Any]) -> None:
        if (cq.get("data") or "").startswith("sk:"):
            self.callbacks.append(cq)
        else:
            self._answer_callback(cq, "That request has expired.")

    # ---- skills: pending changes, commands, the review ------------------

    def send_pending(self, chat_id: int, recs: list[dict[str, Any]] | None = None) -> None:
        recs = self.make_agent.store.pending() if recs is None else recs
        for rec in recs:
            card = skill_tools.pending_card(rec, self.s)
            if len(card) > CARD_DETAIL + 400:
                card = card[:CARD_DETAIL + 400] + "\n... (cut off)"
            self._send(chat_id, card,
                reply_markup={"inline_keyboard": [[
                    {"text": "Approve", "callback_data": f"sk:ok:{rec['id']}"},
                    {"text": "Reject", "callback_data": f"sk:no:{rec['id']}"},
                ]]})

    def handle_skill_button(self, cq: dict[str, Any]) -> None:
        """Approve or reject a pending skill change. These buttons don't expire."""
        parts = (cq.get("data") or "").split(":", 2)
        verdict, pid = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
        store = self.make_agent.store
        if not pid:
            self._answer_callback(cq, "That button is broken.")
            return
        if verdict == "ok":
            result = skill_tools.apply_pending(self.s, store, pid)
        else:
            result = skill_tools.reject_pending(store, pid)
        self._answer_callback(cq, result[:180])
        self._close_buttons(cq, result)

    def skill_command(self, chat_id: int, command: str, arg: str) -> None:
        store = self.make_agent.store
        if command == "/skills":
            found = skills.discover(skills.root(self.s))
            lines = [f"{k.category or 'general'}/{k.name}: {k.description}" for k in found] or ["No skills yet."]
            waiting = len(store.pending())
            if waiting:
                lines.append(f"\n{waiting} change(s) wait for your OK: /pending")
            always = store.always_grants()
            if always:
                lines.append("Always allowed: " + ", ".join(always))
            self._send(chat_id, "\n".join(lines))
        elif command == "/pending":
            recs = store.pending()
            if not recs:
                self._send(chat_id, "No skill changes are waiting.")
            else:
                self.send_pending(chat_id, recs)
        elif command == "/undo":
            if not arg:
                self._send(chat_id, "Usage: /undo <skill name>")
            else:
                self._send(chat_id, skill_tools.undo(self.s, store, arg.split()[0]))

    def memory_command(self, chat_id: int, arg: str) -> None:
        parts = arg.split()
        if not parts:
            text = memory.owner_view(self.s)
            waiting = sum(1 for r in self.make_agent.store.pending() if r["origin"] == memory.ORIGIN)
            if waiting:
                text += f"\n{waiting} memory change(s) wait for your OK: /pending"
            self._send(chat_id, text)
        elif parts[0] == "forget" and len(parts) == 3:
            self._send(chat_id, memory.forget(self.s, parts[1], parts[2]))
        else:
            self._send(chat_id, "Usage: /memory, or /memory forget user|memory N")

    def after_reply(self, chat_id: int, agent: Agent) -> None:
        """The background review, after the reply is out (Hermes runs it the same way)."""
        if not (agent.review_due or agent.memory_review_due):
            return
        try:
            self.bot.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass
        result = review.run(agent)
        self.log(f"[telegram] {result.summary()}")
        if result.saved or result.remembered or result.staged or result.skipped:
            self._send(chat_id, result.summary())
        if result.staged:
            store = self.make_agent.store
            self.send_pending(chat_id, [r for r in store.pending() if r["id"] in result.staged])

    # ---- handling owner messages ---------------------------------------

    def handle(self, m: dict[str, Any]) -> None:
        chat_id = m["chat"]["id"]
        user = m["from"]["id"]
        is_owner = user == self.owner
        self.last_activity = time.monotonic()
        photo_sizes = m.get("photo") or []
        if not isinstance(photo_sizes, list):
            photo_sizes = []
        text = (m.get("text") or m.get("caption") or "").strip()
        if not text and not photo_sizes:
            self._send(chat_id, "I can read text and photos for now.")
            return
        if not text:
            text = "What is in this image?"
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        arg = text.split(None, 1)[1].strip() if command and len(text.split(None, 1)) > 1 else ""
        if command == "/stats":
            args = text.split()[1:]
            if args == ["info"]:
                state = "on" if self.stats_preferences.enabled(chat_id) else "off"
                self._send(chat_id, f"Stats {state}.\n{self.stats.info(self.stats_preferences.fields(chat_id))}\n"
                           f"Preferences: telegram_stats.json. {self.stats_preferences.error}".rstrip(),
                           decorate=False)  # the info body already contains telemetry
                return
            try:
                if args in ([], ["on"], ["off"]):
                    enabled = args == ["on"] if args else not self.stats_preferences.enabled(chat_id)
                    self.stats_preferences.set(chat_id, enabled)
                    selection = ""
                else:
                    self.stats_preferences.update(chat_id, args[0], args[1:])
                    enabled = self.stats_preferences.enabled(chat_id)
                    selection = " Fields: " + (", ".join(self.stats_preferences.fields(chat_id)) or "none") + "."
            except ValueError:
                self._send(chat_id, "Usage: /stats [on|off|info|reset] or /stats add|remove FIELD...\n"
                           "Fields: mem cpu bat swap up disk")
                return
            except OSError:
                self._send(chat_id, "Could not save stats preference; state unchanged.")
                return
            self._send(chat_id, f"Stats {'on' if enabled else 'off'}.{selection}")
            return
        if command in ("/start", "/help"):
            self._send(chat_id, HELP if is_owner else GUEST_HELP)
            return
        if command == "/new":
            sid = self.make_agent.new_session(user)
            self._send(chat_id, f"Started conversation #{sid}.")
            return
        if command == "/stop":
            self._send(chat_id, "Nothing to stop right now.")
            return
        if command in OWNER_ONLY and not is_owner:
            self._send(chat_id, "That command is for the owner only.")
            return
        if command in ("/skills", "/pending", "/undo"):
            self.skill_command(chat_id, command, arg)
            return
        if command == "/approvals":
            self._send(chat_id, approval.manage(self.make_agent.store, arg))
            return
        if command == "/memory":
            self.memory_command(chat_id, arg)
            return
        image_data_url = None
        if photo_sizes:
            largest = photo_sizes[-1]  # Telegram lists available photo sizes smallest first.
            file_id = largest.get("file_id") if isinstance(largest, dict) else None
            if not isinstance(file_id, str) or not file_id:
                self._send(chat_id, "[error] Telegram photo has no file ID.")
                return
            try:
                image_data_url = self.bot.photo_data_url(file_id)
            except TelegramError as e:
                self._send(chat_id, f"[error] Could not fetch photo: {e}")
                return

        api_text, step_limit = None, None
        if command == "/learn":
            api_text, step_limit = learn.build_prompt(arg), LEARN_STEPS
        if is_owner and self.cron is not None:
            # What scheduled jobs sent since the owner last wrote rides in this message, so a
            # reply like "do it" to a reminder has context. The prompt prefix stays untouched.
            notes = cron.take_notes(self.cron.store)
            if notes:
                api_text = f"{notes}\n\n{api_text or text}"

        def on_event(kind: str, detail: str) -> None:
            if kind in ("retry", "wait", "guard", "notice"):
                self.log(f"[telegram] {kind}: {detail}")
            if kind == "wait":  # a long retry wait: say so instead of going quiet for minutes
                try:
                    self._send(chat_id, f"[waiting] {detail}")
                except TelegramError:
                    pass

        if is_owner:
            def approve(req: str) -> str:
                return self.approve(chat_id, req)
        else:
            def approve(req: str) -> bool:
                self.log(f"[telegram] refused risky tool for guest {user}: {req}")
                return False  # guests never get risky tools

        def autorecover(seconds: float, cycle: int, total: int, error: str) -> bool:
            return self.wait_out(chat_id, user, seconds, cycle, total, error)

        agent = self.make_agent(user, approve, on_event, autorecover=autorecover)
        try:
            self.bot.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass
        try:
            reply = agent.run_turn(text, api_text=api_text, step_limit=step_limit,
                                   image_data_url=image_data_url)
        except (provider.ProviderError, SessionBusy) as e:
            self._send(chat_id, f"[error] {e}")
            return
        # Every memory write is shown, so a claimed save that never happened is visible.
        notes = "".join(f"[remembered] {n}\n" for n in agent.memory_notes)
        used = [t for t in dict.fromkeys(agent.tools_ok) if t != "memory"]
        if used:  # only tools that actually ran, not denied or failed calls
            notes = f"[used {', '.join(used)}]\n" + notes
        reply = notes + reply
        self._send(chat_id, reply)
        if is_owner:
            self.after_reply(chat_id, agent)
        self.last_activity = time.monotonic()

    def housekeeping(self) -> None:
        """Between polls: buttons pressed during a wait, scheduled jobs, and the weekly curator."""
        while self.callbacks:
            self.handle_skill_button(self.callbacks.pop(0))
        if self.cron is not None:
            try:
                cron.tick(self.cron.store, self.cron)
            except Exception as e:  # the scheduler must never take the bridge down
                self.log(f"[telegram] cron tick failed: {type(e).__name__}: {e}")
        report = curator.maybe_run(self.s, self.make_agent.store, time.monotonic() - self.last_activity)
        if report:
            self.log(f"[telegram] {report}")
            if "nothing to do" not in report:
                try:
                    self._send(self.owner, report)
                except TelegramError:
                    pass


    def poll_wait(self) -> float:
        """The long-poll wait, cut short when a scheduled job falls due sooner."""
        if self.cron is None:
            return POLL_SECONDS
        try:
            due = cron.seconds_until_due(self.cron.store)
        except Exception:
            return POLL_SECONDS
        return POLL_SECONDS if due is None else max(1, min(POLL_SECONDS, math.ceil(due)))

    def run_forever(self) -> None:
        me = self.bot.call("getMe")
        who = f"owner {self.owner}" + (f" and {len(self.guests)} guest(s)" if self.guests else "")
        self.log(f"Telegram bridge running as @{me.get('username')}; answering only {who}. Ctrl-C stops it.")
        if self.cron is not None:
            try:
                for run in cron.recover(self.cron.store, self.cron):
                    self.log(f"[telegram] cron run {run['id']} of job {run['job_id']} was interrupted")
            except Exception as e:
                self.log(f"[telegram] cron recovery failed: {type(e).__name__}: {e}")
        backoff = 5
        while True:
            try:
                while self.pending:
                    self.handle(self.pending.pop(0))
                self.housekeeping()
                for u in self._fetch(self.poll_wait()):
                    if self._is_owner_message(u):
                        self.handle(u["message"])
                    elif self._is_owner_callback(u):
                        cq = u["callback_query"]
                        if (cq.get("data") or "").startswith("sk:"):
                            self.handle_skill_button(cq)
                        else:
                            self._answer_callback(cq, "That request has expired.")
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
        # Guests' conversations are kept out of session_search; mark the ones already on disk.
        for f in s.data_dir.glob("telegram_session_*"):
            try:
                self.store.set_session_field(int(f.read_text().strip()), "guest", 1)
            except (OSError, ValueError):
                pass

    def _session_file(self, user: int):
        name = "telegram_session" if user == self.owner else f"telegram_session_{user}"
        return self.s.data_dir / name

    def session_for(self, user: int) -> int:
        try:
            sid = int(self._session_file(user).read_text().strip())
            if self.store.session_exists(sid):
                if user != self.owner:
                    self.store.set_session_field(sid, "guest", 1)
                return sid
        except (FileNotFoundError, ValueError):
            pass
        return self.new_session(user)

    def new_session(self, user: int) -> int:
        sid = self.store.new_session()
        if user != self.owner:
            self.store.set_session_field(sid, "guest", 1)
        path = self._session_file(user)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(sid))
        return sid

    def __call__(self, user: int, approve, on_event, autorecover=None) -> Agent:
        # Unattended surface: the loop guard may stop a turn, and outages are waited out
        # (Hermes does both on its gateway).
        return Agent(self.s, self.api_key, self.store, self.session_for(user), approve, on_event,
                     hard_stop=True, autorecover=autorecover, guest=user != self.owner)


def run(s: Settings) -> int:
    token, owner = load_telegram_config(s)
    guests = load_guests(s, owner)
    api_key = load_api_key(s)
    store = Store(s.db_path)
    try:
        def log(line: str) -> None:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", flush=True)

        bridge = Bridge(s, Bot(token), owner, AgentFactory(s, api_key, store, owner), log=log, guests=guests)
        bridge.cron = cron.Runner(s, api_key, store, deliver=lambda text: bridge._send(owner, text), log=log)
        bridge.run_forever()
    finally:
        store.close()
    return 0
