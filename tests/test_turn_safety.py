"""Persist-as-you-go, auto-recovery, compaction, the turn lease and the request deadline.
FAKE model and FAKE local HTTP server only; nothing here talks to DeepSeek."""

from __future__ import annotations

import json
import os
import ssl
import time
import unittest
import urllib.error
from unittest import mock

from chelys import agent as agent_mod
from chelys import compaction, provider
from chelys.config import Settings
from chelys.store import SessionBusy, Store, history_chars
from tests.test_agent import Base, FakeModel, FakeServer, OK_REPLY, tool_call


def transient(msg="model API returned HTTP 503: DeepSeek server error", **kw):
    return provider.ProviderError(msg, retryable=True, transient=True, **kw)


class PersistTests(Base):
    def test_tool_call_row_is_saved_before_the_tool_runs(self):
        seen = []

        def approve(summary):
            seen.append([m["role"] for m in self.store.messages(self.sid)])
            return True

        model = FakeModel([
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("write_file", {"path": str(self.tmp / "a.txt"), "content": "x"})]},
            {"role": "assistant", "content": "done"},
        ])
        self.agent(model, approve=approve).run_turn("write it")
        self.assertEqual(seen, [["user", "assistant"]])
        self.assertEqual([m["role"] for m in self.store.messages(self.sid)], ["user", "assistant", "tool", "assistant"])

    def test_crash_leftover_is_closed_on_the_next_turn(self):
        # what a process killed after the first of two tools leaves behind
        self.store.append_turn(self.sid, [
            agent_mod.stamp_user_message("do two things"),
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("list_dir", {}, "c1"), tool_call("battery_status", {}, "c2")]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
        ])
        events = []
        model = FakeModel([{"role": "assistant", "content": "hello"}])
        a = agent_mod.Agent(self.s, "k", self.store, self.sid, lambda s: False,
                            on_event=lambda k, t: events.append(k), chat_fn=model)
        a.run_turn("hi")
        sent = model.calls[0]
        self.assertEqual([m["role"] for m in sent[1:]], ["user", "assistant", "tool", "tool", "assistant", "user"])
        self.assertEqual(sent[4]["tool_call_id"], "c2")
        self.assertIn("may or may not have run", sent[4]["content"])
        self.assertIn("may already have run", sent[5]["content"])
        self.assertIn("notice", events)
        saved = self.store.messages(self.sid)
        self.assertEqual(saved[:5], sent[1:6])

    def test_crash_before_any_tool_says_so_plainly(self):
        self.store.append_turn(self.sid, [agent_mod.stamp_user_message("hello?")])
        model = FakeModel([{"role": "assistant", "content": "hi"}])
        self.agent(model).run_turn("anyone there")
        note = model.calls[0][2]
        self.assertEqual(note["role"], "assistant")
        self.assertIn("interrupted", note["content"])
        self.assertNotIn("may already have run", note["content"])

    def test_ctrl_c_during_tools_closes_every_call(self):
        def approve(summary):
            raise KeyboardInterrupt

        model = FakeModel([{"role": "assistant", "content": None, "tool_calls": [
            tool_call("write_file", {"path": "a", "content": "x"}, "c1"), tool_call("list_dir", {}, "c2")]}])
        with self.assertRaises(KeyboardInterrupt):
            self.agent(model, approve=approve).run_turn("go")
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant", "tool", "tool", "assistant"])
        self.assertTrue(all(m["content"].startswith("not run") for m in saved[2:4]))
        self.assertIn("cancelled", saved[-1]["content"])

    def test_scaffolding_still_never_saved(self):
        model = FakeModel([dict({"role": "assistant", "content": "part one "}, _finish_reason="length"),
                           {"role": "assistant", "content": "part two"}])
        self.agent(model).run_turn("long answer please")
        saved = self.store.messages(self.sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant"])
        self.assertEqual(saved[-1]["content"], "part one part two")


class RecoveryTests(Base):
    def run_agent(self, replies, recover=True, answer=True):
        waits = []

        def autorecover(wait, cycle, total, error):
            waits.append((wait, cycle, total))
            return answer

        model = FakeModel(replies)
        a = agent_mod.Agent(self.s, "k", self.store, self.sid, lambda s: False, chat_fn=model,
                            autorecover=autorecover if recover else None)
        return a, model, waits

    def test_outage_is_waited_out_on_the_ladder(self):
        a, model, waits = self.run_agent([transient(), transient(), {"role": "assistant", "content": "back"}])
        self.assertEqual(a.run_turn("hi"), "back")
        self.assertEqual([w[1:] for w in waits], [(1, 5), (2, 5)])
        self.assertTrue(15 <= waits[0][0] <= 18 and 30 <= waits[1][0] <= 36, waits)  # 15/30 s + 0-20%

    def test_retry_after_wins_up_to_120s(self):
        a, _, waits = self.run_agent([transient(retry_after=500), {"role": "assistant", "content": "ok"}])
        a.run_turn("hi")
        self.assertEqual(waits[0][0], 120)

    def test_rate_limit_and_client_errors_are_not_waited_out(self):
        for err in (provider.ProviderError("HTTP 429", retryable=True), provider.ProviderError("HTTP 401")):
            a, _, waits = self.run_agent([err])
            with self.assertRaises(provider.ProviderError):
                a.run_turn("hi")
            self.assertEqual(waits, [])

    def test_gives_up_after_the_configured_cycles(self):
        self.s.autorecover_cycles = 2
        a, _, waits = self.run_agent([transient(), transient(), transient()])
        with self.assertRaisesRegex(provider.ProviderError, "gave up after 2 cycles"):
            a.run_turn("hi")
        self.assertEqual(len(waits), 2)
        self.assertIn("gave up", self.store.messages(self.sid)[-1]["content"])

    def test_stop_cancels(self):
        a, _, _ = self.run_agent([transient()], answer=False)
        with self.assertRaisesRegex(provider.ProviderError, "cancelled with /stop"):
            a.run_turn("hi")

    def test_no_callback_means_no_waiting(self):
        a, _, waits = self.run_agent([transient()], recover=False)
        with self.assertRaises(provider.ProviderError):
            a.run_turn("hi")
        self.assertEqual(waits, [])

    def test_the_call_is_retried_not_the_turn(self):
        a, model, _ = self.run_agent([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("list_dir", {})]},
            transient(),
            {"role": "assistant", "content": "listed"},
        ])
        self.assertEqual(a.run_turn("list"), "listed")
        self.assertEqual(a.tools_ok, ["list_dir"])
        self.assertEqual(model.calls[1], model.calls[2], "the same request is sent again")


class LeaseTests(Base):
    def lease(self):
        return self.store.db.execute("SELECT lease_holder FROM sessions WHERE id = ?", (self.sid,)).fetchone()[0]

    def test_busy_session_is_refused_and_nothing_saved(self):
        self.store.acquire_lease(self.sid, f"pid={os.getpid()}:other")
        model = FakeModel([])
        with self.assertRaisesRegex(SessionBusy, "busy"):
            self.agent(model).run_turn("hi")
        self.assertEqual(self.store.messages(self.sid), [])
        self.assertEqual(model.calls, [])

    def test_expired_lease_is_taken_over(self):
        self.store.acquire_lease(self.sid, f"pid={os.getpid()}:other", ttl=-1)
        self.assertEqual(self.agent(FakeModel([{"role": "assistant", "content": "ok"}])).run_turn("hi"), "ok")

    @unittest.skipUnless(os.name == "posix", "dead-process check is POSIX only")
    def test_dead_holder_is_taken_over(self):
        self.store.acquire_lease(self.sid, "pid=999999:gone")
        self.assertEqual(self.agent(FakeModel([{"role": "assistant", "content": "ok"}])).run_turn("hi"), "ok")

    def test_lease_is_released_after_success_and_failure(self):
        self.agent(FakeModel([{"role": "assistant", "content": "ok"}])).run_turn("hi")
        self.assertIsNone(self.lease())
        with self.assertRaises(provider.ProviderError):
            self.agent(FakeModel([provider.ProviderError("HTTP 400")])).run_turn("hi")
        self.assertIsNone(self.lease())

    def test_old_database_is_migrated(self):
        import sqlite3
        path = self.tmp / "old.sqlite3"
        db = sqlite3.connect(str(path))
        db.executescript("""CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
                            updated REAL NOT NULL, title TEXT NOT NULL DEFAULT '');
                            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
                            turn INTEGER NOT NULL, body TEXT NOT NULL);
                            INSERT INTO sessions (created, updated) VALUES (1, 1);
                            INSERT INTO messages (session_id, turn, body) VALUES (1, 1, '{"role": "user", "content": "old"}');""")
        db.commit()
        db.close()
        st = Store(path)
        try:
            self.assertEqual(st.messages(1), [{"role": "user", "content": "old"}])
            st.acquire_lease(1, "pid=1:x")
        finally:
            st.close()


def fill(store, sid, turns, tool_at=None, size=300):
    """Save `turns` ordinary turns; turn `tool_at` reads a big file."""
    for i in range(turns):
        rows = [agent_mod.stamp_user_message(f"question {i} " + "q" * size)]
        if i == tool_at:
            rows += [{"role": "assistant", "content": None,
                      "tool_calls": [tool_call("read_file", {"path": "big.txt"}, f"r{i}")]},
                     {"role": "tool", "tool_call_id": f"r{i}", "content": "B" * 3000}]
        rows.append({"role": "assistant", "content": f"answer {i} " + "a" * size})
        store.append_turn(sid, rows)


class CompactionTests(Base):
    def setUp(self):
        super().setUp()
        self.s.history_chars = 6000

    def test_older_history_is_summarized_not_dropped(self):
        fill(self.store, self.sid, 10, tool_at=3)
        before = self.store.messages(self.sid)
        model = FakeModel([{"role": "assistant", "content": "SUMMARY-1: talked about questions 1-7"},
                           {"role": "assistant", "content": "next answer"}])
        self.assertEqual(self.agent(model).run_turn("next"), "next answer")

        prompt_call = model.calls[0]
        self.assertEqual(len(prompt_call), 1)
        prompt = prompt_call[0]["content"]
        self.assertIn("context checkpoint", prompt)
        self.assertIn("[read_file] read big.txt (3,000 chars)", prompt)   # old tool output shortened
        self.assertNotIn("B" * 300, prompt)
        self.assertIn(compaction.TASK_HEADING, prompt)
        self.assertEqual(model.kwargs[0]["max_tokens"], self.s.max_tokens_limit)

        sent = model.calls[1]
        carrier = next(m for m in sent if compaction.END_MARKER in str(m.get("api_content")))
        self.assertTrue(carrier["api_content"].startswith(compaction.SUMMARY_FRAME))
        self.assertIn("SUMMARY-1", carrier["api_content"])
        wire = provider.build_payload(self.s, sent, [])["messages"]
        self.assertIn("SUMMARY-1", next(m["content"] for m in wire if compaction.END_MARKER in str(m["content"])))
        self.assertEqual(sent[1], before[0], "the first turn (head) is kept verbatim")

        live = self.store.messages(self.sid)
        self.assertGreater(self.store.archived_count(self.sid), 0)
        self.assertLess(history_chars(live), history_chars(before))
        summarized = [m for m in live if "_summary" in m]
        self.assertEqual(len(summarized), 1)
        self.assertNotIn("SUMMARY", summarized[0]["content"], "what the owner typed is untouched")
        self.assertEqual(live[-1]["content"], "next answer")

    def test_second_compaction_updates_the_first_summary(self):
        fill(self.store, self.sid, 10)
        self.agent(FakeModel([{"role": "assistant", "content": "SUMMARY-1"},
                              {"role": "assistant", "content": "a"}])).run_turn("x")
        fill(self.store, self.sid, 8)
        model = FakeModel([{"role": "assistant", "content": "SUMMARY-2"}, {"role": "assistant", "content": "b"}])
        self.agent(model).run_turn("y")
        prompt = model.calls[0][0]["content"]
        self.assertIn("EARLIER CHECKPOINT:\nSUMMARY-1", prompt)
        self.assertNotIn(compaction.SUMMARY_FRAME, prompt, "the old frame is not fed back in")
        live = self.store.messages(self.sid)
        self.assertEqual([m["_summary"] for m in live if "_summary" in m], ["SUMMARY-2"])

    def test_failed_summary_changes_nothing_and_pauses(self):
        fill(self.store, self.sid, 10)
        before = self.store.messages(self.sid)
        model = FakeModel([provider.ProviderError("HTTP 500"), {"role": "assistant", "content": "still here"}])
        self.assertEqual(self.agent(model).run_turn("x"), "still here")
        self.assertEqual(self.store.archived_count(self.sid), 0)
        self.assertEqual(self.store.messages(self.sid)[:len(before)], before)
        self.assertGreater(self.store.compact_retry_at(self.sid), time.time() + 500)
        model2 = FakeModel([{"role": "assistant", "content": "no summary attempt"}])
        self.agent(model2).run_turn("y")
        self.assertEqual(len(model2.calls), 1)

    def test_cut_off_summary_is_not_used(self):
        fill(self.store, self.sid, 10)
        model = FakeModel([dict({"role": "assistant", "content": "SUMMARY half"}, _finish_reason="length"),
                           {"role": "assistant", "content": "ok"}])
        self.agent(model).run_turn("x")
        self.assertEqual(self.store.archived_count(self.sid), 0)

    def test_small_history_and_disabled_setting_do_nothing(self):
        fill(self.store, self.sid, 2)
        model = FakeModel([{"role": "assistant", "content": "ok"}])
        self.agent(model).run_turn("x")
        self.assertEqual(len(model.calls), 1)
        fill(self.store, self.sid, 10)
        self.s.compaction = False
        model = FakeModel([{"role": "assistant", "content": "ok"}])
        self.agent(model).run_turn("x")
        self.assertEqual(len(model.calls), 1)


class CompactionUnitTests(unittest.TestCase):
    def test_plan_keeps_whole_turns(self):
        hist = []
        for i in range(12):
            hist += [{"role": "user", "content": "u" * 400}, {"role": "assistant", "content": "a" * 400}]
        head_end, tail_start = compaction.plan(hist, 5000)
        self.assertEqual(hist[head_end]["role"], "user")
        self.assertEqual(hist[tail_start]["role"], "user")
        self.assertGreaterEqual(head_end, compaction.PROTECT_FIRST_N)
        self.assertLessEqual(history_chars(hist[tail_start:]), 2500)
        self.assertIsNone(compaction.plan(hist[:4], 5000))

    def test_head_protection_ends_after_first_summary(self):
        hist = [{"role": "user", "content": "u" * 400, "_summary": "s"}, {"role": "assistant", "content": "a" * 400}]
        hist += [{"role": "user", "content": "u" * 400}, {"role": "assistant", "content": "a" * 400}] * 8
        self.assertEqual(compaction.plan(hist, 4000)[0], 0)

    def test_redaction(self):
        text = ("key sk-abcdefghijklmnopqrstuvwx, bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ, "
                "password: hunter2xyz, Authorization: Bearer abc.def.ghijklmnop")
        out = compaction.redact(text)
        for secret in ("sk-abcdef", "AAHdqTcv", "hunter2xyz", "abc.def.ghij"):
            self.assertNotIn(secret, out)
        self.assertIn("password: [REDACTED]", out)

    def test_failed_tool_never_summarized_as_success(self):
        self.assertIn("FAILED", compaction.tool_one_liner("write_file", {"path": "x"}, "error: is a directory: x"))
        self.assertIn("FAILED", compaction.tool_one_liner("write_file", {"path": "x"}, "denied: the owner did not approve"))
        self.assertIn("-> exit 3", compaction.tool_one_liner("run_shell", {"command": "false"}, "exit code 3\nstderr:\nx"))


class DeadlineTests(unittest.TestCase):
    def test_trickling_response_hits_the_total_deadline(self):
        body = json.dumps(OK_REPLY).encode()

        def trickle(h):
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            try:
                for b in body:
                    h.wfile.write(bytes([b]))
                    h.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

        srv = FakeServer(trickle)
        try:
            s = Settings(base_url=srv.url, request_timeout=5, request_deadline=0.5, api_max_retries=0)
            start = time.monotonic()
            with self.assertRaisesRegex(provider.ProviderError, "longer than 0.5s in total") as cm:
                provider.chat(s, "k", [{"role": "user", "content": "hi"}], [])
            self.assertLess(time.monotonic() - start, 3)
            self.assertTrue(cm.exception.transient)
        finally:
            srv.close()

    def test_certificate_failure_is_not_retried(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        s = Settings(api_max_retries=3)
        with mock.patch("urllib.request.urlopen", side_effect=err) as op:
            with self.assertRaisesRegex(provider.ProviderError, "TLS certificate check failed"):
                provider.chat(s, "k", [{"role": "user", "content": "hi"}], [], sleep=lambda w: None)
        self.assertEqual(op.call_count, 1)

    def test_give_up_keeps_the_error_kind(self):
        s = Settings(api_max_retries=1)
        with mock.patch("chelys.provider._chat_once", side_effect=transient()):
            with self.assertRaises(provider.ProviderError) as cm:
                provider.chat(s, "k", [], [], sleep=lambda w: None)
        self.assertTrue(cm.exception.transient)
        self.assertIn("gave up after 1 retries", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
