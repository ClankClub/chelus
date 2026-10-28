"""Telegram bridge tests with a FAKE Telegram API and a FAKE model. No network."""

from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MEMINFO = "MemTotal: 1748992 kB\nMemAvailable: 520192 kB\nSwapTotal: 1572864 kB\nSwapFree: 958464 kB\n"
FOOTER = "mem 1.2/1.7G"

from chelys import provider, telegram
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
        self.downloaded_photos = []
        self.image_data_url = "data:image/jpeg;base64,ZmFrZQ=="

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

    def photo_data_url(self, file_id):
        self.downloaded_photos.append(file_id)
        return self.image_data_url

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

    def run_bridge(self, script, replies, guests=(), meminfo="", cpu_sample=""):
        model = FakeModel(replies)

        class Factory(telegram.AgentFactory):
            def __call__(self, user, approve, on_event, **kw):
                a = super().__call__(user, approve, on_event, **kw)
                a.chat_fn = model
                return a

        self.factory = Factory(self.s, "k", self.store, OWNER)
        bot = FakeBot(script)
        logs = []
        bridge = telegram.Bridge(self.s, bot, OWNER, self.factory, log=logs.append, guests=set(guests))
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                return meminfo
            if str(path) == "/data/local/tmp/chelys-telemetry.json":
                return cpu_sample
            return read_text(path, *args, **kwargs)
        with patch.object(Path, "read_text", read), self.assertRaises(KeyboardInterrupt):
            bridge.run_forever()
        self.assertFalse([line for line in logs if "unexpected" in line], logs)
        self.assertEqual(model.replies, [], "all scripted model replies must be consumed")
        return bot, model, bridge

    def test_skill_commands_and_guest_help_keep_runtime_footer(self):
        bot, model, _ = self.run_bridge([[msg(1, OWNER, "/skills"), msg(2, OWNER, "/pending"),
                                         msg(3, GUEST, "/skills"), msg(4, GUEST, "/help")]], [],
                                       guests={GUEST}, meminfo=MEMINFO)
        self.assertTrue(all(t.endswith(FOOTER) for t in bot.texts()), bot.texts())
        self.assertIn("/stats", bot.texts()[-1])
        self.assertNotIn("/learn", bot.texts()[-1])
        self.assertEqual(model.calls, [])

    def test_pending_skill_card_keeps_footer_and_buttons(self):
        bot = FakeBot([])
        factory = telegram.AgentFactory(self.s, "k", self.store, OWNER)
        bridge = telegram.Bridge(self.s, bot, OWNER, factory)
        bridge.stats.footer = lambda fields: FOOTER
        with patch("chelys.telegram.skill_tools.pending_card", return_value="Skill change"):
            bridge.send_pending(OWNER, [{"id": "synthetic"}])
        self.assertEqual(bot.texts(), ["Skill change\n\n" + FOOTER])
        self.assertTrue(any("reply_markup" in params for method, params in bot.sent))

    def test_runtime_footer_default_owner_and_guest(self):
        bot, model, _ = self.run_bridge(
            [[msg(1, OWNER, "hi"), msg(2, GUEST, "hello")]],
            [{"role": "assistant", "content": "hello"}] * 2,
            guests={GUEST}, meminfo=MEMINFO)
        self.assertEqual(bot.texts(), ["hello\n\n" + FOOTER] * 2)
        self.assertEqual(len(model.calls), 2)

    def test_stats_controls_persist_per_chat_without_model_history(self):
        commands = ["/stats", "/stats on", "/stats off", "/new", "/stats info"]
        bot, model, _ = self.run_bridge([[msg(i, OWNER, t) for i, t in enumerate(commands, 1)]
                                        + [msg(10, GUEST, "/stats off"), msg(11, OWNER, "hi")]],
                                       [{"role": "assistant", "content": "hello"}],
                                       guests={GUEST}, meminfo=MEMINFO)
        self.assertEqual(bot.texts()[0], "Stats off.")
        self.assertEqual(bot.texts()[1], "Stats on.\n\n" + FOOTER)
        self.assertEqual(bot.texts()[2], "Stats off.")
        self.assertIn("Stats off.", bot.texts()[4])
        self.assertIn("MemTotal - MemAvailable", bot.texts()[4])
        self.assertIn(FOOTER, bot.texts()[4])
        self.assertEqual(len(model.calls), 1)
        self.assertNotIn("/stats", json.dumps(model.calls))
        self.assertNotIn(FOOTER, json.dumps(model.calls))
        state = self.s.data_dir / "telegram_stats.json"
        self.assertEqual(json.loads(state.read_text()), {
            str(chat): {"enabled": False, "fields": ["mem", "cpu", "bat"]}
            for chat in (OWNER, GUEST)})
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        bot, _, _ = self.run_bridge([[msg(12, OWNER, "again"), msg(13, GUEST, "/stats")]],
                                    [{"role": "assistant", "content": "again"}], guests={GUEST}, meminfo=MEMINFO)
        self.assertEqual(bot.texts(), ["again", "Stats on.\n\n" + FOOTER])

    def test_stats_field_controls_are_local_persistent_and_resettable(self):
        commands = ["/stats add swap up disk", "/stats remove cpu", "/stats info", "/stats off",
                    "/stats reset", "/stats remove mem cpu bat", "/stats info"]
        with patch("chelys.stats.os.statvfs", side_effect=PermissionError("denied")):
            bot, model, bridge = self.run_bridge([[msg(i, OWNER, t) for i, t in enumerate(commands, 1)]
                                                 + [msg(20, GUEST, "/stats add swap")]], [],
                                                 guests={GUEST}, meminfo=MEMINFO)
        self.assertIn(FOOTER + " | swap 0.6/1.5G", bot.texts()[0])
        self.assertIn("Selected fields: mem, bat, swap, up, disk", bot.texts()[2])
        self.assertEqual(bot.texts()[3], "Stats off.")
        self.assertEqual(bot.texts()[4], "Stats on. Fields: mem, cpu, bat.\n\n" + FOOTER)
        self.assertEqual(bot.texts()[5], "Stats on. Fields: none.")
        self.assertIn("Selected fields: none", bot.texts()[6])
        self.assertEqual(bridge.stats_preferences.fields(OWNER), ())
        self.assertEqual(bridge.stats_preferences.fields(GUEST), ("mem", "cpu", "bat", "swap"))
        self.assertEqual(model.calls, [])
        self.assertEqual(self.store.messages(self.factory.session_for(OWNER)), [])
        self.assertEqual(telegram.StatsPreferences(self.s.data_dir).fields(OWNER), ())

    def test_invalid_stats_field_commands_do_not_write(self):
        commands = ["/stats add", "/stats remove", "/stats add swap nope", "/stats reset cpu",
                    "/stats remove cpu 1111", "/stats unknown", "/stats on cpu"]
        bot, model, _ = self.run_bridge([[msg(i, GUEST, t) for i, t in enumerate(commands, 1)]], [],
                                       guests={GUEST}, meminfo=MEMINFO)
        self.assertTrue(all("Usage:" in t for t in bot.texts()))
        self.assertFalse((self.s.data_dir / "telegram_stats.json").exists())
        self.assertEqual(model.calls, [])

    def test_footer_commands_errors_approval_edits_and_outage(self):
        target = self.tmp / "approved.txt"
        bot, model, bridge = self.run_bridge([
            [msg(1, OWNER, "/help"), msg(2, OWNER, "write")], press(OWNER, "ok"),
            [msg(3, OWNER, "fail")]], [
                {"role": "assistant", "content": None, "tool_calls": [tool_call("write_file", {"path": str(target), "content": "x"})]},
                {"role": "assistant", "content": "done"}, provider.ProviderError("bad key")], meminfo=MEMINFO)
        self.assertTrue(all(t.endswith(FOOTER) for t in bot.texts()))
        edits = [p["text"] for method, p in bot.sent if method == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertTrue(edits[0].endswith(FOOTER))
        bridge.wait_out(OWNER, OWNER, 0, 1, 5, "down")
        self.assertTrue(bot.texts()[-1].endswith(FOOTER))
        self.assertNotIn(FOOTER, json.dumps(model.calls))
        self.assertNotIn(FOOTER, json.dumps(self.store.messages(self.factory.session_for(OWNER))))

    def test_stats_info_explains_missing_metrics_without_toggle(self):
        bot, model, _ = self.run_bridge([[msg(1, OWNER, "/stats info"), msg(2, OWNER, "/stats")]], [])
        self.assertIn("RAM unavailable", bot.texts()[0])
        self.assertIn("Swap unavailable", bot.texts()[0])
        self.assertIn("Stats on.", bot.texts()[0])
        self.assertEqual(bot.texts()[1], "Stats off.")
        self.assertEqual(model.calls, [])

    def test_long_footer_chunks_preserve_body_and_limits(self):
        text = "a" * 3950 + "\n\n\n" + "😀" * 4100 + "\n\nend\n"
        bot, _, _ = self.run_bridge([[msg(1, OWNER, "long")]],
                                   [{"role": "assistant", "content": text}], meminfo=MEMINFO)
        suffix = "\n\n" + FOOTER
        self.assertTrue(all(t.endswith(suffix) for t in bot.texts()))
        self.assertTrue(all(len(t.encode("utf-16-le")) // 2 <= telegram.MAX_TEXT for t in bot.texts()))
        self.assertEqual("".join(t[:-len(suffix)] for t in bot.texts()), text)

    def test_stats_save_failure_reports_unchanged(self):
        with patch("chelys.stats.os.replace", side_effect=PermissionError("denied")):
            bot, model, bridge = self.run_bridge([[msg(1, OWNER, "/stats off")]], [], meminfo=MEMINFO)
        self.assertIn("Could not save", bot.texts()[0])
        self.assertIn("unchanged", bot.texts()[0])
        self.assertTrue(bridge.stats_preferences.enabled(OWNER))
        self.assertEqual(model.calls, [])

    def test_stats_authorization_and_guest_cannot_target_other_chat(self):
        bot, model, _ = self.run_bridge([[msg(1, STRANGER, "/stats off"), msg(2, OWNER, "/stats off", "group"),
                                         msg(3, GUEST, f"/stats off {OWNER}"), msg(4, OWNER, "/stats info")]], [],
                                       guests={GUEST}, meminfo=MEMINFO)
        self.assertEqual(len(bot.texts()), 2)
        self.assertIn("Usage:", bot.texts()[0])
        self.assertIn("Stats on.", bot.texts()[1])
        self.assertEqual(model.calls, [])
        self.assertFalse((self.s.data_dir / "telegram_stats.json").exists())

    def test_long_approval_edit_preserves_all_content_with_footer(self):
        bot = FakeBot([press(OWNER, "ok")])
        bridge = telegram.Bridge(self.s, bot, OWNER, None)
        summary = "😀" * 4200 + "\n\nend"
        with patch.object(Path, "read_text", return_value=MEMINFO):
            self.assertTrue(bridge.approve(OWNER, summary))
        suffix = "\n\n" + FOOTER
        edits = [p["text"] for method, p in bot.sent if method == "editMessageText"]
        all_payloads = bot.texts() + edits
        self.assertTrue(all(len(t.encode("utf-16-le")) // 2 <= telegram.MAX_TEXT for t in all_payloads))
        approved = edits + [p["text"] for method, p in bot.sent
                            if method == "sendMessage" and "reply_markup" not in p]
        self.assertEqual("".join(t[:-len(suffix)] for t in approved), "Allowed once: " + summary)

    def test_cpu_cache_file_reaches_footer_not_model_or_history(self):
        cache = self.tmp / "synthetic-telemetry.json"
        cache.write_text(json.dumps({"version": 1, "timestamp": time.time(), "cpu_percent": 12.5, "cpu_temp_c": 31.2}))
        with patch("chelys.stats.CPU_CACHE_PATH", cache):
            bot, model, _ = self.run_bridge([[msg(1, OWNER, "hi"), msg(2, OWNER, "/stats off"),
                                             msg(3, OWNER, "/stats info")]],
                                           [{"role": "assistant", "content": "hello"}], meminfo=MEMINFO)
        self.assertEqual(bot.texts()[0], "hello\n\n" + FOOTER + " | cpu 12.5% - 31°C")
        self.assertEqual(bot.texts()[1], "Stats off.")
        self.assertIn("12.5%", bot.texts()[2])
        self.assertIn("privileged", bot.texts()[2])
        self.assertNotIn("CPU", json.dumps(model.calls))
        self.assertNotIn(FOOTER, json.dumps(self.store.messages(self.factory.session_for(OWNER))))

    def test_help_lists_stats_controls(self):
        bot, model, _ = self.run_bridge([[msg(1, OWNER, "/help")]], [])
        self.assertIn("/stats", bot.texts()[0])
        self.assertIn("on|off|info", bot.texts()[0])
        self.assertEqual(model.calls, [])

    def test_stats_info_does_not_duplicate_telemetry(self):
        bot, _, _ = self.run_bridge([[msg(1, OWNER, "/stats info")]], [], meminfo=MEMINFO)
        self.assertEqual(bot.texts()[0].count(FOOTER), 1)

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

    def test_failed_tool_is_not_in_used_tag(self):
        bot, _, _ = self.run_bridge([[msg(1, OWNER, "read it")]], [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("read_file", {"path": "nope.txt"})]},
            {"role": "assistant", "content": "no such file"},
        ])
        self.assertEqual(bot.texts(), ["no such file"])

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

    def test_photo_caption_reaches_model_but_is_not_persisted(self):
        update = msg(1, OWNER, "")
        update["message"].pop("text")
        update["message"]["caption"] = "What is in this image?"
        update["message"]["photo"] = [{"file_id": "small"}, {"file_id": "largest"}]
        bot, model, _ = self.run_bridge([[update]], [
            {"role": "assistant", "content": "A test image."},
        ])
        self.assertEqual(bot.downloaded_photos, ["largest"])
        user_msg = next(row for row in model.calls[0] if row.get("role") == "user")
        parts = user_msg["api_content"]
        self.assertEqual([part["type"] for part in parts], ["text", "image_url"])
        self.assertIn("What is in this image?", parts[0]["text"])
        self.assertEqual(parts[1]["image_url"]["url"], bot.image_data_url)
        payload = provider.build_payload(self.s, [user_msg], [])
        self.assertEqual(payload["messages"][0]["content"], parts)
        history = self.store.messages(self.factory.session_for(OWNER))
        self.assertNotIn(bot.image_data_url, json.dumps(history))

    def test_photo_without_caption_gets_default_prompt(self):
        update = msg(1, OWNER, "")
        update["message"].pop("text")
        update["message"]["photo"] = [{"file_id": "only-photo"}]
        bot, model, _ = self.run_bridge([[update]], [
            {"role": "assistant", "content": "A test image."},
        ])
        self.assertEqual(bot.downloaded_photos, ["only-photo"])
        user_msg = next(row for row in model.calls[0] if row.get("role") == "user")
        self.assertIn("What is in this image?", user_msg["api_content"][0]["text"])

    def test_approval_yes_runs_tool(self):
        target = self.tmp / "made.txt"
        bot, _, _ = self.run_bridge([[msg(1, OWNER, "write it")], press(OWNER, "ok")], [
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(target), "content": "hey"})]},
            {"role": "assistant", "content": "done"},
        ])
        self.assertEqual(target.read_text(encoding="utf-8"), "hey")
        self.assertTrue(any(t.startswith("Allow this?\nCreate a file on the phone") for t in bot.texts()))
        self.assertIn(("editMessageText", {"chat_id": OWNER, "message_id": 5,
                                           "text": f"Allowed once: Create a file on the phone\nFile: {target}"}),
                      bot.sent)

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

    def test_outage_countdown_then_recovery(self):
        down = provider.ProviderError("HTTP 503", retryable=True, transient=True, retry_after=0)
        bot, _, _ = self.run_bridge([[msg(1, OWNER, "hi")]], [down, {"role": "assistant", "content": "back"}])
        self.assertTrue(bot.texts()[0].startswith("[waiting] DeepSeek is unavailable (HTTP 503)"))
        self.assertIn("/stop", bot.texts()[0])
        self.assertEqual(bot.texts()[-1], "back")

    def test_stop_cancels_the_wait(self):
        down = provider.ProviderError("HTTP 503", retryable=True, transient=True)
        bot, model, _ = self.run_bridge([[msg(1, OWNER, "hi")], [msg(2, OWNER, "/stop")]], [down])
        self.assertIn("Stopped retrying.", bot.texts())
        self.assertIn("cancelled with /stop", bot.texts()[-1])
        self.assertEqual(len(model.calls), 1)

    def test_stop_with_nothing_running(self):
        bot, _, _ = self.run_bridge([[msg(1, OWNER, "/stop")]], [])
        self.assertEqual(bot.texts(), ["Nothing to stop right now."])

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

    def test_photo_download_encodes_data_url(self):
        bot = telegram.Bot("1:fake", timeout_pad=2, api="https://example.invalid")

        class Response:
            def __init__(self):
                self.requested = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                self.requested = size
                return b"fake-jpeg"

        response = Response()
        with patch.object(bot, "call", return_value={
                "file_path": "photos/test.jpg", "file_size": 9}) as get_file,              patch("chelys.telegram.urllib.request.urlopen", return_value=response) as download:
            data_url = bot.photo_data_url("photo-id")

        get_file.assert_called_once_with("getFile", {"file_id": "photo-id"})
        self.assertEqual(data_url, "data:image/jpeg;base64," + base64.b64encode(b"fake-jpeg").decode("ascii"))
        self.assertEqual(download.call_args.args[0].full_url,
                         "https://example.invalid/file/bot1:fake/photos/test.jpg")
        self.assertEqual(response.requested, telegram.MAX_IMAGE_BYTES + 1)

    def test_oversized_photo_rejected_before_download(self):
        bot = telegram.Bot("1:fake", api="https://example.invalid")
        with patch.object(bot, "call", return_value={
                "file_path": "photos/large.jpg", "file_size": telegram.MAX_IMAGE_BYTES + 1}),              patch("chelys.telegram.urllib.request.urlopen") as download:
            with self.assertRaisesRegex(telegram.TelegramError, "exceeds"):
                bot.photo_data_url("photo-id")
        download.assert_not_called()

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
