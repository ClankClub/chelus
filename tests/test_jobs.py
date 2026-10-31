"""Background shell runs (jobs.py) and run_shell's timeout and background modes. The tests
that start real processes need POSIX; they run on the phone."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chelus import approval, jobs, tools
from tests.test_agent import make_settings

POSIX = os.name == "posix"


def wait_done(s, job_id, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = jobs.status(s, job_id)
        if job["state"] == "done":
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} still running after {seconds}s")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp)

    def tearDown(self):
        for job in jobs.all_jobs(self.s):
            if job["state"] == "running":
                try:
                    jobs.kill(self.s, job["id"])
                except jobs.JobError:
                    pass
        self._tmp.cleanup()

    def fake_job(self, job_id, *, pid=None, started=None, done=None, log=b""):
        folder = jobs._folder(self.s)
        meta = {"id": job_id, "command": "echo x", "purpose": "", "cwd": str(self.tmp),
                "started": time.time() if started is None else started, "limit": jobs.LIMIT_S, "pid": pid}
        (folder / f"{job_id}.json").write_text(json.dumps(meta))
        (folder / f"{job_id}.log").write_bytes(log)
        if done is not None:
            (folder / f"{job_id}.done").write_text(json.dumps(done))


class ShellModeTests(Base):
    def test_default_is_120_seconds_in_the_foreground(self):
        self.assertEqual(self.s.shell_timeout, 120.0)
        self.assertEqual(tools.shell_mode(self.s, {"command": "x"}), (False, 120.0))
        self.assertEqual(tools.shell_mode(self.s, {"command": "x", "timeout": 45}), (False, 45.0))

    def test_long_timeout_or_flag_goes_to_the_background(self):
        self.assertEqual(tools.shell_mode(self.s, {"timeout": 600}), (True, float(jobs.LIMIT_S)))
        self.assertTrue(tools.shell_mode(self.s, {"background": True})[0])
        self.assertTrue(tools.shell_mode(self.s, {"background": "true"})[0])
        self.assertFalse(tools.shell_mode(self.s, {"background": False})[0])

    def test_bad_arguments_are_refused(self):
        for args in ({"timeout": 0}, {"timeout": "soon"}, {"timeout": 1.5}, {"background": "yes"}):
            with self.assertRaises(tools.ToolError):
                tools.shell_mode(self.s, args)

    def test_card_and_grant_tell_background_apart(self):
        fg = {"command": "pkg upgrade -y", "purpose": "update"}
        bg = dict(fg, background=True)
        card = tools.describe_call("run_shell", bg, self.s)
        self.assertIn("in the background", card.title)
        self.assertIn("30 minutes", card.card())
        self.assertNotIn("background", tools.describe_call("run_shell", fg, self.s).title)
        promoted = tools.describe_call("run_shell", dict(fg, timeout=900), self.s)
        self.assertIn("in the background", promoted.title)
        self.assertNotEqual(tools.action_key(self.s, "run_shell", dict(fg)),
                            tools.action_key(self.s, "run_shell", dict(bg)))
        self.assertEqual(tools.action_key(self.s, "run_shell", dict(fg)),
                         tools.action_key(self.s, "run_shell", dict(fg, timeout=30)))
        self.assertIn(approval.ALWAYS, card.choices)

    def test_guests_get_no_background_runs_or_job_tool(self):
        self.assertNotIn("shell_jobs", tools.available(guest=True))
        self.assertIn("shell_jobs", tools.available())
        with self.assertRaises(tools.ToolError):
            tools.TOOLS["run_shell"].handler(self.s, {"command": "true", "background": True},
                                             SimpleNamespace(guest=True))


class StateTests(Base):
    def test_lost_when_the_worker_is_gone(self):
        self.fake_job("aaaaaa", pid=None, started=time.time() - 3600)
        job = jobs.status(self.s, "aaa")
        self.assertEqual((job["state"], job["reason"]), ("done", "lost"))
        self.assertIn("restarted", jobs.describe(job))

    def test_unknown_and_ambiguous_ids(self):
        self.fake_job("abc111", done={"code": 0, "reason": "exit", "ended": time.time()})
        self.fake_job("abc222", done={"code": 0, "reason": "exit", "ended": time.time()})
        with self.assertRaises(jobs.JobError):
            jobs.status(self.s, "zzz")
        with self.assertRaises(jobs.JobError):
            jobs.status(self.s, "abc")
        with self.assertRaises(jobs.JobError):
            jobs.status(self.s, "../x")
        self.assertEqual(jobs.status(self.s, "abc1")["id"], "abc111")

    def test_running_limit(self):
        for i in range(jobs.MAX_RUNNING):
            self.fake_job(f"00000{i}", pid=os.getpid())
        with self.assertRaises(jobs.JobError) as cm:
            jobs.start(self.s, "true")
        self.assertIn("already running", str(cm.exception))

    def test_notice_is_redacted_cut_and_sent_once(self):
        log = ("\n".join(f"line {i}" for i in range(100)) + "\napi_key=abcdef1234567890\n").encode()
        self.fake_job("bbbbbb", done={"code": 2, "reason": "exit", "ended": time.time()}, log=log)
        sent, notes = [], []
        n = jobs.notify(self.s, lambda t: sent.append(t) or 42, note=notes.append, log=lambda _: None)
        self.assertEqual(n, 1)
        self.assertEqual(sent, notes)
        text = sent[0]
        self.assertIn("exit code 2", text)
        self.assertIn("line 99", text)
        self.assertNotIn("line 50", text)
        self.assertNotIn("abcdef1234567890", text)
        self.assertEqual(jobs.notify(self.s, lambda t: 1, log=lambda _: None), 0)
        self.assertTrue(jobs.status(self.s, "bbbbbb")["told"])

    def test_failed_send_still_keeps_the_note(self):
        self.fake_job("cccccc", done={"code": 0, "reason": "exit", "ended": time.time()})
        notes, logs = [], []

        def boom(_):
            raise OSError("network down")
        jobs.notify(self.s, boom, note=notes.append, log=logs.append)
        self.assertEqual(len(notes), 1)
        self.assertTrue(logs)
        jobs.notify(self.s, lambda t: None, note=notes.append, log=logs.append)
        self.assertEqual(len(notes), 1)  # not resent

    def test_running_jobs_get_no_notice(self):
        self.fake_job("dddddd", pid=os.getpid())
        with patch.object(jobs, "_alive", return_value=True):
            self.assertEqual(jobs.collect(self.s), [])
            self.assertIn("running", jobs.listing(self.s))

    def test_prune_keeps_recent(self):
        old = time.time() - jobs.KEEP_S - 10
        self.fake_job("eeeeee", started=old, done={"code": 0, "reason": "exit", "ended": old})
        self.fake_job("ffffff", done={"code": 0, "reason": "exit", "ended": time.time()})
        jobs.prune(self.s)
        ids = [j["id"] for j in jobs.all_jobs(self.s)]
        self.assertEqual(ids, ["ffffff"])

    def test_log_is_capped_with_head_and_tail(self):
        path = self.tmp / "cap.log"
        log = jobs._Log(path)
        for i in range(800):
            log.write(f"{i:06d}".encode() + b"x" * 1017 + b"\n")  # 1 KB lines
        log.close()
        data = path.read_bytes()
        self.assertLessEqual(len(data), jobs.LOG_BYTES + jobs.SLACK_BYTES + 200)
        self.assertTrue(data.startswith(b"000000"))
        self.assertIn(b"bytes of output dropped here", data)
        self.assertTrue(data.rstrip().endswith(b"x"))
        self.assertIn(b"000799", data)

    def test_shell_jobs_tool(self):
        self.fake_job("123abc", done={"code": 0, "reason": "exit", "ended": time.time()}, log=b"hello\n")
        run = tools.TOOLS["shell_jobs"].handler
        self.assertIn("123abc", run(self.s, {"action": "list"}))
        self.assertIn("hello", run(self.s, {"action": "log", "id": "123"}))
        with self.assertRaises(tools.ToolError):
            run(self.s, {"action": "kill", "id": "123abc"})  # not running
        with self.assertRaises(tools.ToolError):
            run(self.s, {"action": "dance"})


@unittest.skipUnless(POSIX, "starts real background processes")
class LiveTests(Base):
    def test_runs_in_the_background_and_reports_exit(self):
        out = tools.TOOLS["run_shell"].handler(
            self.s, {"command": "echo started; sleep 1; echo finished; exit 3", "background": True,
                     "purpose": "test"}, SimpleNamespace(guest=False))
        self.assertTrue(out.startswith("started background job"))
        job_id = out.split()[3]
        self.assertEqual(jobs.status(self.s, job_id)["state"], "running")
        job = wait_done(self.s, job_id)
        self.assertEqual((job["code"], job["reason"]), (3, "exit"))
        self.assertIn("finished", jobs.read_log(self.s, job_id))
        texts = jobs.collect(self.s)
        self.assertEqual(len(texts), 1)
        self.assertIn("exit code 3", texts[0])
        self.assertIn("Why: test", texts[0])

    def test_kill_stops_the_whole_group(self):
        job = jobs.start(self.s, "sleep 60 & sleep 60; echo never")
        time.sleep(0.5)
        job = jobs.kill(self.s, job["id"])
        self.assertEqual((job["state"], job["reason"]), ("done", "killed"))
        self.assertNotIn("never", jobs.read_log(self.s, job["id"]))

    def test_limit_stops_the_run(self):
        with patch.object(jobs, "LIMIT_S", 2):
            job = jobs.start(self.s, "echo begun; sleep 30")
        job = wait_done(self.s, job["id"])
        self.assertEqual(job["reason"], "timeout")
        self.assertIn("begun", jobs.read_log(self.s, job["id"]))

    def test_worker_outlives_a_left_behind_child(self):
        # The command exits but leaves something holding the output pipe open.
        job = jobs.start(self.s, "(sleep 20 &) ; echo quick")
        job = wait_done(self.s, job["id"], seconds=10)
        self.assertEqual((job["code"], job["reason"]), (0, "exit"))

    def test_foreground_timeout_message_suggests_background(self):
        self.s.shell_timeout = 1.0
        with self.assertRaises(tools.ToolError) as cm:
            tools.TOOLS["run_shell"].handler(self.s, {"command": "sleep 5"}, None)
        self.assertIn("background=true", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
