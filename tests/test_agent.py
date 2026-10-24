"""Tests run with a FAKE model (scripted replies) and a FAKE local HTTP server.
Nothing here talks to DeepSeek; passing these is not proof of a live provider turn."""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from chelys import agent as agent_mod
from chelys import provider, tools
from chelys.config import ConfigError, Settings, load_api_key, load_settings
from chelys.store import Store, recent_window


def make_settings(tmp: Path, **kw) -> Settings:
    s = Settings(config_dir=tmp / "cfg", data_dir=tmp / "data", workdir=str(tmp))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def tool_call(name: str, args: dict, cid: str = "call_1") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeModel:
    """Returns scripted assistant messages and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, s, key, messages, schemas):
        self.calls.append(json.loads(json.dumps(messages)))
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return dict(r)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp)
        self.store = Store(self.s.db_path)
        self.sid = self.store.new_session()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def agent(self, model, approve=lambda summary: False):
        return agent_mod.Agent(self.s, "test-key", self.store, self.sid, approve, chat_fn=model)


class AgentLoopTests(Base):
    def test_tool_call_then_final_answer(self):
        (self.tmp / "hello.txt").write_text("phone says hi", encoding="utf-8")
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("read_file", {"path": "hello.txt"})]},
            {"role": "assistant", "content": "It says hi."},
        ])
        reply = self.agent(model).run_turn("what's in hello.txt?")
        self.assertEqual(reply, "It says hi.")
        tool_msg = model.calls[1][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["content"], "phone says hi")
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant", "tool", "assistant"])

    def test_tool_failure_goes_back_to_model(self):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("read_file", {"path": "nope.txt"})]},
            {"role": "assistant", "content": "No such file."},
        ])
        self.agent(model).run_turn("read nope.txt")
        self.assertTrue(model.calls[1][-1]["content"].startswith("error: not a file"))

    def test_bad_arguments_and_unknown_tool(self):
        bad = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}}
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [bad, tool_call("launch_rocket", {}, "c2")]},
            {"role": "assistant", "content": "ok"},
        ])
        self.agent(model).run_turn("go")
        results = [m["content"] for m in model.calls[1] if m["role"] == "tool"]
        self.assertTrue(results[0].startswith("error: bad arguments"))
        self.assertTrue(results[1].startswith("error: no tool named"))

    def test_iteration_limit(self):
        self.s.max_iterations = 3
        loop = {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]}
        model = FakeModel([loop, loop, loop, loop])
        reply = self.agent(model).run_turn("loop forever")
        self.assertIn("limit of 3", reply)
        self.assertEqual(len(model.calls), 3)
        saved = self.store.messages(self.sid)
        self.assertEqual(saved[-1]["role"], "assistant")

    def test_tool_output_is_clipped(self):
        self.s.tool_output_chars = 50
        (self.tmp / "big.txt").write_text("x" * 500, encoding="utf-8")
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("read_file", {"path": "big.txt"})]},
            {"role": "assistant", "content": "big"},
        ])
        self.agent(model).run_turn("read big")
        content = model.calls[1][-1]["content"]
        self.assertTrue(content.startswith("x" * 50))
        self.assertIn("cut off: 450 more", content)

    def test_risky_tool_denied(self):
        target = self.tmp / "should_not_exist.txt"
        model = FakeModel([
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(target), "content": "x"})]},
            {"role": "assistant", "content": "ok, didn't write"},
        ])
        self.agent(model, approve=lambda summary: False).run_turn("write a file")
        self.assertFalse(target.exists())
        self.assertTrue(model.calls[1][-1]["content"].startswith("denied"))

    def test_risky_tool_approved(self):
        target = self.tmp / "out.txt"
        asked = []
        model = FakeModel([
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(target), "content": "hey"})]},
            {"role": "assistant", "content": "written"},
        ])
        self.agent(model, approve=lambda summary: asked.append(summary) or True).run_turn("write")
        self.assertEqual(target.read_text(encoding="utf-8"), "hey")
        self.assertIn("write_file", asked[0])

    def test_provider_error_saves_note_not_half_turn(self):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]},
            provider.ProviderError("model request timed out after 60s"),
        ])
        with self.assertRaises(provider.ProviderError):
            self.agent(model).run_turn("list stuff")
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant"])
        self.assertIn("timed out", saved[1]["content"])
        self.assertIn("list_dir", saved[1]["content"])

    def test_ctrl_c_saves_note_and_reraises(self):
        model = FakeModel([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.agent(model).run_turn("hello")
        saved = self.store.messages(self.sid)
        self.assertIn("cancelled", saved[-1]["content"])

    def test_resume_after_restart(self):
        model = FakeModel([{"role": "assistant", "content": "Noted: your cat is Biscuit."}])
        self.agent(model).run_turn("my cat is Biscuit")
        self.store.close()
        self.store = Store(self.s.db_path)  # simulate a fresh process
        self.assertEqual(self.store.latest_session(), self.sid)
        model2 = FakeModel([{"role": "assistant", "content": "Biscuit."}])
        self.agent(model2).run_turn("what's my cat called?")
        sent = model2.calls[0]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("my cat is Biscuit", [m.get("content") for m in sent])


class HistoryWindowTests(unittest.TestCase):
    def test_window_starts_at_user_and_keeps_newest(self):
        hist = []
        for i in range(10):
            hist += [{"role": "user", "content": f"q{i}" * 50},
                     {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {}, f"c{i}")]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": "r" * 100},
                     {"role": "assistant", "content": "a"}]
        win = recent_window(hist, 1500)
        self.assertEqual(win[0]["role"], "user")
        self.assertLess(len(win), len(hist))
        self.assertEqual(win[-1], hist[-1])
        self.assertEqual(recent_window(hist, 10)[0]["content"], "q9" * 50)


class ProviderShapeTests(unittest.TestCase):
    def test_thinking_off_payload(self):
        s = Settings()
        msgs = [{"role": "assistant", "content": "", "reasoning_content": "hmm",
                 "tool_calls": [tool_call("list_dir", {})]}]
        p = provider.build_payload(s, msgs, tools.schemas())
        self.assertEqual(p["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_content", p["messages"][0])
        self.assertIsNone(p["messages"][0]["content"])

    def test_thinking_on_pads_reasoning(self):
        s = Settings(thinking=True)
        msgs = [{"role": "assistant", "content": "hi"}]
        p = provider.build_payload(s, msgs, [])
        self.assertEqual(p["messages"][0]["reasoning_content"], " ")
        self.assertNotIn("tools", p)


class FakeServer:
    """Local stand-in for the DeepSeek API: plain HTTP on 127.0.0.1."""

    def __init__(self, handler_fn):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.last_request = (self.path, dict(self.headers), json.loads(body))
                handler_fn(self)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def respond(code, obj):
    def fn(h):
        data = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    return fn


class ProviderHttpTests(unittest.TestCase):
    def run_with(self, handler, timeout=5.0):
        srv = FakeServer(handler)
        try:
            s = Settings(base_url=srv.url, request_timeout=timeout)
            return provider.chat(s, "sk-test", [{"role": "user", "content": "hi"}], tools.schemas()), srv
        finally:
            srv.close()

    def test_success_with_tool_call(self):
        msg, srv = self.run_with(respond(200, {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": [tool_call("battery_status", {})]}}]}))
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "battery_status")
        path, headers, body = srv.last_request
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(body["model"], "deepseek-flash")

    def test_401(self):
        with self.assertRaisesRegex(provider.ProviderError, "401.*key was rejected"):
            self.run_with(respond(401, {"error": {"message": "Authentication Fails"}}))

    def test_500(self):
        with self.assertRaisesRegex(provider.ProviderError, "500.*server error"):
            self.run_with(respond(500, {}))

    def test_malformed(self):
        with self.assertRaisesRegex(provider.ProviderError, "unexpected response"):
            self.run_with(respond(200, {"nope": 1}))

    def test_timeout(self):
        def slow(h):
            time.sleep(2)
            try:
                respond(200, {})(h)
            except OSError:
                pass  # client already gave up, which is the point
        with self.assertRaisesRegex(provider.ProviderError, "timed out"):
            self.run_with(slow, timeout=0.5)

    def test_unreachable(self):
        s = Settings(base_url="http://127.0.0.1:9", request_timeout=3)
        with self.assertRaises(provider.ProviderError):
            provider.chat(s, "k", [{"role": "user", "content": "hi"}], [])


class ConfigTests(unittest.TestCase):
    def test_missing_key(self):
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d))
            old = os.environ.pop("DEEPSEEK_API_KEY", None)
            try:
                with self.assertRaisesRegex(ConfigError, "no DeepSeek API key"):
                    load_api_key(s)
            finally:
                if old is not None:
                    os.environ["DEEPSEEK_API_KEY"] = old

    def test_key_file(self):
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d))
            s.config_dir.mkdir(parents=True)
            (s.config_dir / "deepseek.key").write_text("sk-abc\n", encoding="utf-8")
            old = os.environ.pop("DEEPSEEK_API_KEY", None)
            try:
                self.assertEqual(load_api_key(s), "sk-abc")
            finally:
                if old is not None:
                    os.environ["DEEPSEEK_API_KEY"] = old

    def test_config_toml(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d)
            (cfg / "config.toml").write_text('model = "deepseek-v4-pro"\nrequest_timeout = 30\n', encoding="utf-8")
            os.environ["CHELYS_CONFIG_DIR"] = str(cfg)
            try:
                s = load_settings()
                self.assertEqual(s.model, "deepseek-v4-pro")
                self.assertEqual(s.request_timeout, 30.0)
                (cfg / "config.toml").write_text('max_iterations = "lots"\n', encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_settings()
                (cfg / "config.toml").write_text('bogus = 1\n', encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_settings()
            finally:
                del os.environ["CHELYS_CONFIG_DIR"]


@unittest.skipUnless(os.name == "posix", "shell tool needs a POSIX shell")
class ShellToolTests(unittest.TestCase):
    def test_shell_timeout_kills(self):
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d), shell_timeout=1.0)
            start = time.monotonic()
            with self.assertRaisesRegex(tools.ToolError, "timed out"):
                tools.TOOLS["run_shell"].handler(s, {"command": "sleep 30 & sleep 30"})
            self.assertLess(time.monotonic() - start, 10)

    def test_shell_output(self):
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d))
            out = tools.TOOLS["run_shell"].handler(s, {"command": "echo hi; echo oops >&2; exit 3"})
            self.assertIn("exit code 3", out)
            self.assertIn("hi", out)
            self.assertIn("oops", out)


if __name__ == "__main__":
    unittest.main()
