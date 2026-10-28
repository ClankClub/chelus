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
from unittest.mock import patch
from pathlib import Path

from chelus import agent as agent_mod
from chelus import guardrails, provider, tools
from chelus.config import ConfigError, Settings, load_api_key, load_settings
from chelus.repetition import is_repetition_dominated
from chelus.store import Store, recent_window


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
        self.kwargs = []

    def __call__(self, s, key, messages, schemas, **kw):
        self.kwargs.append(kw)
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
        bad = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{not json}"}}
        cut = {"id": "c3", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "a", "content": "lo'}}
        empty = {"id": "c4", "type": "function", "function": {"name": "list_dir", "arguments": ""}}
        model = FakeModel([
            {"role": "assistant", "content": None,
             "tool_calls": [bad, tool_call("launch_rocket", {}, "c2"), cut, empty]},
            {"role": "assistant", "content": "ok"},
        ])
        self.agent(model).run_turn("go")
        results = [m["content"] for m in model.calls[1] if m["role"] == "tool"]
        self.assertTrue(results[0].startswith("error: bad arguments"))
        self.assertTrue(results[1].startswith("error: no tool named"))
        self.assertIn("Available tools: list_dir", results[1])
        self.assertIn("cut off before the end", results[2])
        self.assertFalse(results[3].startswith("error"), "empty-string arguments mean {}")

    def test_iteration_limit_asks_for_summary(self):
        self.s.max_iterations = 3
        loop = {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]}
        model = FakeModel([loop, loop, loop, {"role": "assistant", "content": "I listed the folder three times."}])
        reply = self.agent(model).run_turn("loop forever")
        self.assertEqual(reply, "I listed the folder three times.")
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(model.calls[3][-1]["content"], agent_mod.SUMMARY_REQUEST)
        self.assertTrue(model.calls[3][0] == model.calls[0][0], "grace call keeps the same system prompt")
        saved = self.store.messages(self.sid)
        self.assertEqual(saved[-1], {"role": "assistant", "content": "I listed the folder three times."})

    def test_iteration_limit_summary_fails(self):
        self.s.max_iterations = 1
        loop = {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]}
        model = FakeModel([loop, provider.ProviderError("HTTP 500")])
        reply = self.agent(model).run_turn("loop")
        self.assertIn("ran out of steps", reply)

    def test_iteration_limit_empty_summary_retried_once(self):
        self.s.max_iterations = 1
        loop = {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]}
        empty = {"role": "assistant", "content": ""}
        model = FakeModel([loop, empty, empty])
        self.assertEqual(self.agent(model).run_turn("loop"), agent_mod.EMPTY_SUMMARY)
        self.assertEqual(len(model.calls), 3)

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

    def test_provider_error_keeps_what_ran_and_adds_note(self):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]},
            provider.ProviderError("model request timed out after 60s"),
        ])
        with self.assertRaises(provider.ProviderError):
            self.agent(model).run_turn("list stuff")
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant", "tool", "assistant"])
        self.assertIn("timed out", saved[-1]["content"])
        self.assertIn("list_dir", saved[-1]["content"])
        self.assertIn("may already have run", saved[-1]["content"])

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


def length(msg: dict) -> dict:
    """A scripted reply that hit max_tokens."""
    return dict(msg, _finish_reason="length")


class CacheAndUsageTests(Base):
    def test_system_prompt_is_stable_and_time_rides_in_user_message(self):
        model = FakeModel([{"role": "assistant", "content": "one"}, {"role": "assistant", "content": "two"}])
        self.agent(model).run_turn("first")
        self.agent(model).run_turn("second")
        first, second = model.calls
        self.assertEqual(first[0], second[0], "system prompt must be byte-identical across turns")
        self.assertNotIn("Local time", first[0]["content"])
        self.assertRegex(first[1]["api_content"], r"^first\n\n\[sent \d{4}-\d\d-\d\d \d\d:\d\d")
        # turn 2 replays turn 1 exactly as sent, so the cached prefix still matches
        self.assertEqual(second[1], first[1])
        sent = provider.build_payload(self.s, second, [])["messages"]
        self.assertEqual(sent[1]["content"], first[1]["api_content"])
        self.assertEqual(self.store.messages(self.sid)[0]["content"], "first")

    def test_usage_is_logged(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 50,
                 "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}
        events = []
        model = FakeModel([{"role": "assistant", "content": "hi", "_usage": usage}])
        a = agent_mod.Agent(self.s, "k", self.store, self.sid, lambda s: False,
                            on_event=lambda k, t: events.append((k, t)), chat_fn=model)
        a.run_turn("hello")
        self.assertEqual(self.store.usage_totals(self.sid),
                         {"calls": 1, "prompt": 1000, "completion": 50, "cache_hit": 900, "cache_miss": 100})
        self.assertIn(("usage", "1000 in (900 cached), 50 out"), events)
        self.assertNotIn("_usage", self.store.messages(self.sid)[-1])


class TruncationTests(Base):
    def test_cut_off_tool_call_is_never_run(self):
        target = self.tmp / "half.txt"
        cut = length({"role": "assistant", "content": None,
                      "tool_calls": [tool_call("write_file", {"path": str(target), "content": "x"})]})
        model = FakeModel([cut] * 5)
        reply = self.agent(model, approve=lambda s: True).run_turn("write a big file")
        self.assertFalse(target.exists())
        self.assertEqual(reply, agent_mod.TRUNCATED_FINAL)
        self.assertEqual([k["max_tokens"] for k in model.kwargs], [None, 4096, 8192, 16384, 32768])
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant"])
        self.assertNotIn("tool_calls", saved[1])

    def test_no_identical_retry_once_cap_is_maxed(self):
        self.s.max_tokens_limit = 8192
        cut = length({"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]})
        model = FakeModel([cut] * 3)
        self.assertEqual(self.agent(model).run_turn("list"), agent_mod.TRUNCATED_FINAL)
        # a 4th request at 8192 would be identical to the 3rd, so it isn't sent
        self.assertEqual([k["max_tokens"] for k in model.kwargs], [None, 4096, 8192])

    def test_cut_off_tool_call_recovers_with_more_room(self):
        call = {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]}
        model = FakeModel([length(call), call, {"role": "assistant", "content": "done"}])
        self.assertEqual(self.agent(model).run_turn("list"), "done")
        self.assertEqual([k["max_tokens"] for k in model.kwargs], [None, 4096, None])

    def test_text_cut_off_is_continued_and_saved_as_one_reply(self):
        model = FakeModel([
            length({"role": "assistant", "content": "The first half"}),
            {"role": "assistant", "content": "and the second half."},
        ])
        reply = self.agent(model).run_turn("tell me a long thing")
        self.assertEqual(reply, "The first half\nand the second half.")
        self.assertEqual(model.calls[1][-1]["content"], agent_mod.CONTINUE_NUDGE)
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant"])
        self.assertEqual(saved[1]["content"], reply)

    def test_continuation_ceiling(self):
        model = FakeModel([length({"role": "assistant", "content": f"part{i} "}) for i in range(4)])
        reply = self.agent(model).run_turn("go on forever")
        self.assertEqual(len(model.calls), 4)
        self.assertTrue(reply.startswith("part0 part1 part2 part3"))
        self.assertIn("still hitting max_tokens", reply)

    def test_repetition_loop_is_dropped_not_continued(self):
        loop = "I will now check the battery status for you.\n" * 40
        model = FakeModel([length({"role": "assistant", "content": loop})])
        reply = self.agent(model).run_turn("battery?")
        self.assertEqual(reply, agent_mod.REPETITION_FINAL)
        self.assertEqual(len(model.calls), 1)
        self.assertNotIn(loop[:40], json.dumps(self.store.messages(self.sid)))


class ToolCallFixTests(Base):
    def test_duplicate_calls_in_one_reply_are_dropped(self):
        a = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x", "max_bytes": 5}'}}
        b = {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"max_bytes":5,"path":"x"}'}}
        model = FakeModel([{"role": "assistant", "content": None, "tool_calls": [a, b]},
                           {"role": "assistant", "content": "ok"}])
        self.agent(model).run_turn("read x")
        sent = model.calls[1]
        self.assertEqual(len(sent[-2]["tool_calls"]), 1)
        self.assertEqual([m["tool_call_id"] for m in sent if m["role"] == "tool"], ["c1"])

    def test_empty_reply_after_tools_gets_one_nudge(self):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "Here are the files."},
        ])
        reply = self.agent(model).run_turn("list")
        self.assertEqual(reply, "Here are the files.")
        self.assertEqual(model.calls[2][-1]["content"], agent_mod.EMPTY_AFTER_TOOLS_NUDGE)
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant", "tool", "assistant"])

    def test_empty_twice_gives_honest_message(self):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "  "},
        ])
        self.assertIn("returned an empty reply", self.agent(model).run_turn("list"))
        self.assertEqual(len(model.calls), 3)

    def test_tools_ok_counts_only_tools_that_ran(self):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [
                tool_call("read_file", {"path": "missing.txt"}, "c1"),
                tool_call("write_file", {"path": "w.txt", "content": "x"}, "c2"),
                tool_call("list_dir", {}, "c3")]},
            {"role": "assistant", "content": "ok"},
        ])
        a = self.agent(model, approve=lambda s: False)
        a.run_turn("do things")
        self.assertEqual(a.tools_ok, ["list_dir"])


class GuardTests(Base):
    def test_repeated_failure_gets_a_warning(self):
        call = {"role": "assistant", "content": None, "tool_calls": [tool_call("read_file", {"path": "nope"})]}
        model = FakeModel([call, call, {"role": "assistant", "content": "gave up"}])
        self.agent(model).run_turn("read nope")
        second = model.calls[2][-1]["content"]
        self.assertIn("Tool loop warning: repeated_exact_failure_warning; count=2", second)

    def test_identical_streak_halts_when_hard_stop(self):
        loop = {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]}
        model = FakeModel([loop] * 8)
        a = agent_mod.Agent(self.s, "k", self.store, self.sid, lambda s: False, chat_fn=model, hard_stop=True)
        reply = a.run_turn("loop")
        self.assertTrue(reply.startswith("I stopped because I kept running list_dir 5 times"), reply)
        self.assertEqual(len(model.calls), 5)
        saved = self.store.messages(self.sid)
        self.assertEqual(saved[-1]["content"], reply)
        self.assertIn("Tool loop hard stop: identical_call_streak_halt", saved[-2]["content"])

    def test_no_halt_without_hard_stop(self):
        self.s.max_iterations = 6
        loop = {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]}
        model = FakeModel([loop] * 6 + [{"role": "assistant", "content": "summary"}])
        self.assertEqual(self.agent(model).run_turn("loop"), "summary")
        notes = [m["content"] for m in model.calls[-1] if m["role"] == "tool"]
        self.assertIn("3rd consecutive identical call", notes[2])

    def test_identical_big_result_becomes_stub(self):
        (self.tmp / "big.txt").write_text("y" * 600, encoding="utf-8")
        call = {"role": "assistant", "content": None, "tool_calls": [tool_call("read_file", {"path": "big.txt"})]}
        model = FakeModel([call, call, {"role": "assistant", "content": "same"}])
        self.agent(model).run_turn("read twice")
        results = [m["content"] for m in model.calls[2] if m["role"] == "tool"]
        self.assertTrue(results[0].startswith("y" * 600))
        self.assertTrue(results[1].startswith("[note: this result is byte-identical"), results[1])


class GuardUnitTests(unittest.TestCase):
    def test_cycle_detection(self):
        g = guardrails.Guard(hard_stop=True)
        notices = []
        for i in range(10):
            tool = "list_dir" if i % 2 == 0 else "battery_status"
            notices.append(g.observe_call(tool, {}, f"result-{tool}", f"c{i}", False)[0])
        self.assertTrue(any("cycle of 2 tool calls" in n for n in notices))
        self.assertEqual(g.halt.code, "identical_cycle_halt")

    def test_mutation_resets_exact_failure_count(self):
        g = guardrails.Guard(hard_stop=True)
        for _ in range(5):
            g.after_call("read_file", {"path": "a"}, "error: x", True)
        self.assertFalse(g.before_call("read_file", {"path": "a"}).allows_execution)
        g2 = guardrails.Guard(hard_stop=True)
        for _ in range(5):
            g2.after_call("read_file", {"path": "a"}, "error: x", True)
        g2.after_call("write_file", {"path": "a", "content": "y"}, "wrote 1 bytes", False)
        self.assertTrue(g2.before_call("read_file", {"path": "a"}).allows_execution)


class RepetitionTests(unittest.TestCase):
    def test_detects_loops_and_passes_normal_text(self):
        self.assertFalse(is_repetition_dominated("short " * 10))
        self.assertTrue(is_repetition_dominated("the same line again\n" * 50))
        para = "".join(f"Sentence number {i} is different from the others. " for i in range(12))
        self.assertTrue(is_repetition_dominated(para * 6))  # a long unit repeated exactly
        normal = " ".join(f"word{i}" for i in range(400))
        self.assertFalse(is_repetition_dominated(normal))


class HistoryWindowTests(unittest.TestCase):
    def test_window_start_stays_put_between_jumps(self):
        hist, starts = [], []
        for i in range(60):
            hist += [{"role": "user", "content": f"question {i} " + "q" * 80},
                     {"role": "assistant", "content": "a" * 120}]
            win = recent_window(hist, 3000)
            starts.append(win[0]["content"])
            self.assertLessEqual(sum(len(json.dumps(m)) for m in win), 3000)
        moved = [x != y for x, y in zip(starts, starts[1:])]
        self.assertTrue(any(moved))
        # a turn here is ~290 chars and a block is 750, so the start must hold for 2+ turns per jump
        self.assertFalse(any(a and b for a, b in zip(moved, moved[1:])), "the start moved on consecutive turns")

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


OK_REPLY = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1}}


def sequence(*handlers):
    """Serve each handler once, in order (the last one repeats)."""
    state = {"n": 0}

    def fn(h):
        i = min(state["n"], len(handlers) - 1)
        state["n"] += 1
        handlers[i](h)
    return fn


def respond_headers(code, obj, headers):
    def fn(h):
        data = json.dumps(obj).encode()
        h.send_response(code)
        for k, v in headers.items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    return fn


class ProviderRetryTests(unittest.TestCase):
    def run_with(self, handler, retries=3):
        srv = FakeServer(handler)
        waits, notes = [], []
        try:
            s = Settings(base_url=srv.url, request_timeout=5, api_max_retries=retries)
            try:
                msg = provider.chat(s, "k", [{"role": "user", "content": "hi"}], [],
                                    on_retry=lambda *a: notes.append(a), sleep=waits.append)
            except provider.ProviderError as e:
                msg = e
            return msg, waits, notes
        finally:
            srv.close()

    def test_5xx_then_success(self):
        msg, waits, notes = self.run_with(sequence(respond(503, {}), respond(502, {}), respond(200, OK_REPLY)))
        self.assertEqual(msg["content"], "hi")
        self.assertEqual(len(waits), 2)
        self.assertTrue(2 <= waits[0] <= 3 and 4 <= waits[1] <= 6, waits)  # 2*2^(n-1) plus 0-50% jitter
        self.assertEqual(notes[0][:2], (1, 3))

    def test_retry_after_header_wins(self):
        msg, waits, _ = self.run_with(sequence(respond_headers(429, {}, {"Retry-After": "7"}), respond(200, OK_REPLY)))
        self.assertEqual(waits, [7.0])

    def test_retry_after_in_body_and_cap(self):
        _, waits, _ = self.run_with(sequence(respond(429, {"error": {"message": "slow", "retry_after": 9999}}),
                                             respond(200, OK_REPLY)))
        self.assertEqual(waits, [provider.RETRY_AFTER_CAP_S])

    def test_client_errors_are_not_retried(self):
        for code in (400, 401, 402, 403, 404):
            err, waits, _ = self.run_with(respond(code, {"error": {"message": "nope"}}))
            self.assertIsInstance(err, provider.ProviderError)
            self.assertEqual(waits, [], code)

    def test_gives_up_after_max_retries(self):
        err, waits, _ = self.run_with(respond(500, {}))
        self.assertEqual(len(waits), 3)
        self.assertIn("gave up after 3 retries", str(err))

    def test_garbled_200_is_retried(self):
        msg, waits, _ = self.run_with(sequence(respond(200, {"nope": 1}), respond(200, OK_REPLY)))
        self.assertEqual(msg["content"], "hi")
        self.assertEqual(len(waits), 1)

    def test_backoff_numbers(self):
        for n, (lo, hi) in {1: (2, 3), 2: (4, 6), 3: (8, 12), 6: (60, 90), 9: (60, 90)}.items():
            w = provider.jittered_backoff(n)
            self.assertTrue(lo <= w <= hi, (n, w))
        self.assertEqual(provider.parse_retry_after("12"), 12.0)
        self.assertIsNone(provider.parse_retry_after("soon"))
        self.assertGreater(provider.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 0)


class ProviderHttpTests(unittest.TestCase):
    def run_with(self, handler, timeout=5.0):
        srv = FakeServer(handler)
        try:
            s = Settings(base_url=srv.url, request_timeout=timeout, api_max_retries=0)
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
        s = Settings(base_url="http://127.0.0.1:9", request_timeout=3, api_max_retries=0)
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
            with patch.dict(os.environ, {"CHELUS_CONFIG_DIR": str(cfg)}):
                s = load_settings()
                self.assertEqual(s.model, "deepseek-v4-pro")
                self.assertEqual(s.request_timeout, 30.0)
                (cfg / "config.toml").write_text('max_iterations = "lots"\n', encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_settings()
                (cfg / "config.toml").write_text('bogus = 1\n', encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_settings()


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
