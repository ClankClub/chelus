"""Telegram bridge tests with a FAKE Telegram API and a FAKE model. No network."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chelys import telegram
from chelys.config import ConfigError
from chelys.store import Store
from tests.test_agent import FakeModel, make_settings, tool_call

OWNER, STRANGER, GUEST = 1111, 2222, 3333


def msg(update_id, user, text, chat_type="private"):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text}}


class FakeBot:
    """Stands in for telegram.Bot. getUpdates returns scripted batches; the script can be a
    function so it can react to what the bridge sent (e.g. press the button it just offered)."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []      # (method, params)

    def call(self, method, params=None, wait=0):
        self.sent.append((method, params or {}))
        if method == "getUpdates":
            if not self.script:
                raise KeyboardInterrupt  # ends run_forever in tests
            step = self.script.pop(0)
            return step(self) if callable(step) else step
        if method == "getMe":
            return {"username": "fake_bot"}
        return True

    def send(self, chat_id, text, **extra):
        for chunk in telegram.split_text(text):
            self.call("sendMessage", {"chat_id": chat_id, "text": chunk, **extra})

    def texts(self):
        return [p["text"] for m, p in self.sent if m == "sendMessage"]

    def last_nonce(self):
        for m, p in reversed(self.sent):
            if m == "sendMessage" and "reply_markup" in p:
                return p["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
        raise AssertionError("no approval request was sent")


def press(user, verdict, nonce_from_bot=True, nonce="beef"):
    def step(bot):
        n = bot.last_nonce() if nonce_from_bot else nonce
        return [{"update_id": 900 + len(bot.sent), "callback_query": {
            "id": "cb", "from": {"id": user}, "data": f"{verdict}:{n}",
            "message": {"chat": {"id": OWNER}, "message_id": 5}}}]
    return step


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp)
        self.store = Store(self.s.db_path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def run_bridge(self, script, replies, guests=()):
        model = FakeModel(replies)

        class Factory(telegram.AgentFactory):
            def __call__(self, user, approve, on_event):
                a = super().__call__(user, approve, on_event)
                a.chat_fn = model
                return a

        self.factory = Factory(self.s, "k", self.store, OWNER)
        bot = FakeBot(script)
        bridge = telegram.Bridge(self.s, bot, OWNER, self.factory, log=lambda *a: None, guests=set(guests))
        with self.assertRaises(KeyboardInterrupt):
            bridge.run_forever()
        return bot, model, bridge

    def test_guest_chats_in_own_conversation(self):
        bot, model, _ = self.run_bridge([[msg(1, OWNER, "I am the owner"), msg(2, GUEST, "hi from guest")]], [
            {"role": "assistant", "content": "hello owner"},
            {"role": "assistant", "content": "hello guest"},
        ], guests={GUEST})
        self.assertEqual(bot.texts(), ["hello owner", "hello guest"])
        guest_sent = [m.get("content") for m in model.calls[1]]
        self.assertNotIn("I am the owner", guest_sent)
        self.assertNotEqual(self.factory.session_for(OWNER), self.factory.session_for(GUEST))

    def test_guest_never_gets_risky_tools(self):
        target = self.tmp / "guest.txt"
        bot, model, _ = self.run_bridge([[msg(1, GUEST, "write a file")]], [
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(target), "content": "x"})]},
            {"role": "assistant", "content": "can't do that"},
        ], guests={GUEST})
        self.assertFalse(target.exists())
        self.assertTrue(model.calls[1][-1]["content"].startswith("denied"))
        self.assertFalse(any("reply_markup" in p for m, p in bot.sent), "no approval buttons for guests")

    def test_guest_safe_tool_works(self):
        bot, _, _ = self.run_bridge([[msg(1, GUEST, "what's here?")]], [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]},
            {"role": "assistant", "content": "some files"},
        ], guests={GUEST})
        self.assertEqual(bot.texts(), ["[used list_dir]\nsome files"])

    def test_stranger_is_ignored(self):
        bot, model, bridge = self.run_bridge(
            [[msg(1, STRANGER, "run rm -rf"), msg(2, OWNER, "hi", chat_type="group")]], [])
        self.assertEqual(model.calls, [])
        self.assertEqual(bot.texts(), [])
        self.assertEqual(bridge.ignored, 2)

    def test_owner_gets_reply_with_tool_tag(self):
        bot, model, _ = self.run_bridge([[msg(1, OWNER, "battery?")]], [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]},
            {"role": "assistant", "content": "all good"},
        ])
        self.assertEqual(bot.texts(), ["[used list_dir]\nall good"])

    def test_approval_yes_runs_tool(self):
        target = self.tmp / "made.txt"
        bot, _, _ = self.run_bridge([[msg(1, OWNER, "write it")], press(OWNER, "ok")], [
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(target), "content": "hey"})]},
            {"role": "assistant", "content": "done"},
        ])
        self.assertEqual(target.read_text(encoding="utf-8"), "hey")
        self.assertTrue(any(t.startswith("Allow this?") for t in bot.texts()))
        self.assertIn(("editMessageText", {"chat_id": OWNER, "message_id": 5,
                                           "text": f"Approved: write_file: {str(target)!r} (3 chars)"}), bot.sent)

    def test_approval_no_and_stranger_button_and_stale_nonce(self):
        target = self.tmp / "nope.txt"
        bot, model, bridge = self.run_bridge([
            [msg(1, OWNER, "write it")],
            press(STRANGER, "ok"),                        # someone else pressing: ignored
            press(OWNER, "ok", nonce_from_bot=False),     # old button: rejected as expired
            press(OWNER, "no"),
        ], [
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(target), "content": "x"})]},
            {"role": "assistant", "content": "ok, skipped"},
        ])
        self.assertFalse(target.exists())
        self.assertTrue(model.calls[1][-1]["content"].startswith("denied"))
        self.assertGreaterEqual(bridge.ignored, 1)

    def test_approval_times_out(self):
        old = telegram.APPROVAL_SECONDS
        telegram.APPROVAL_SECONDS = 0
        try:
            bot, model, _ = self.run_bridge([[msg(1, OWNER, "shell please")]], [
                {"role": "assistant", "content": None, "tool_calls": [tool_call("run_shell", {"command": "echo hi"})]},
                {"role": "assistant", "content": "fine"},
            ])
        finally:
            telegram.APPROVAL_SECONDS = old
        self.assertIn("No answer in time, so that action was denied.", bot.texts())
        self.assertTrue(model.calls[1][-1]["content"].startswith("denied"))

    def test_message_during_approval_is_handled_after(self):
        target = self.tmp / "q.txt"
        bot, model, _ = self.run_bridge([
            [msg(1, OWNER, "write it")],
            [msg(2, OWNER, "also, hello")],
            press(OWNER, "ok"),
        ], [
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(target), "content": "x"})]},
            {"role": "assistant", "content": "written"},
            {"role": "assistant", "content": "hello back"},
        ])
        self.assertEqual(bot.texts()[-2:], ["[used write_file]\nwritten", "hello back"])

    def test_new_and_help_commands(self):
        bot, model, _ = self.run_bridge([[msg(1, OWNER, "/help"), msg(2, OWNER, "/new")]], [])
        self.assertEqual(model.calls, [])
        self.assertTrue(bot.texts()[0].startswith("Chelys"))
        self.assertTrue(bot.texts()[1].startswith("Started conversation #"))

    def test_session_survives_restart(self):
        f1 = telegram.AgentFactory(self.s, "k", self.store, OWNER)
        f2 = telegram.AgentFactory(self.s, "k", self.store, OWNER)
        self.assertEqual(f1.session_for(OWNER), f2.session_for(OWNER))
        self.assertEqual(f1.session_for(GUEST), f2.session_for(GUEST))


class HelperTests(unittest.TestCase):
    def test_split_text(self):
        parts = telegram.split_text("a" * 3000 + "\n" + "b" * 3000)
        self.assertEqual(parts, ["a" * 3000, "b" * 3000])
        self.assertTrue(all(len(p) <= telegram.MAX_TEXT for p in telegram.split_text("x" * 9000)))

    def test_token_never_in_errors(self):
        token = "123456:SECRET-token-value"
        bot = telegram.Bot(token, timeout_pad=2, api="http://127.0.0.1:9")
        with self.assertRaises(telegram.TelegramError) as cm:
            bot.call("getMe")
        self.assertNotIn("SECRET", str(cm.exception))

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d))
            s.config_dir.mkdir(parents=True)
            with self.assertRaisesRegex(ConfigError, "no Telegram bot token"):
                telegram.load_telegram_config(s)
            (s.config_dir / "telegram.token").write_text("123:abc")
            with self.assertRaisesRegex(ConfigError, "no owner user ID"):
                telegram.load_telegram_config(s)
            (s.config_dir / "telegram_user_id").write_text("@me")
            with self.assertRaisesRegex(ConfigError, "numeric"):
                telegram.load_telegram_config(s)
            (s.config_dir / "telegram_user_id").write_text("42\n")
            self.assertEqual(telegram.load_telegram_config(s), ("123:abc", 42))

    def test_guests_file(self):
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d))
            s.config_dir.mkdir(parents=True)
            self.assertEqual(telegram.load_guests(s, 42), set())
            (s.config_dir / "telegram_guests").write_text("# girlfriend\n555  # her\n\n42\n777\n")
            self.assertEqual(telegram.load_guests(s, 42), {555, 777})  # owner is never a guest
            (s.config_dir / "telegram_guests").write_text("@someone\n")
            with self.assertRaisesRegex(ConfigError, "line 1"):
                telegram.load_guests(s, 42)


if __name__ == "__main__":
    unittest.main()
