"""Scheduled jobs: claims, catch-up, the unattended runner and the bridge tick. No network."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chelus import approval, cron, provider, schedule, tools
from chelus.schedule import local_epoch
from chelus.store import Store
from tests.test_agent import FakeModel, make_settings, tool_call

T0 = local_epoch(2026, 10, 28, 8, 0)  # a Wednesday morning


def reply(text):
    return {"role": "assistant", "content": text}


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, text):
        if self.fail:
            raise OSError("network down")
        self.sent.append(text)


class CronBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp)
        self.store = Store(self.s.db_path)
        self.out = Recorder()
        self.lines = []

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def runner(self, model, deliver=None, **kw):
        return cron.Runner(self.s, "test-key", self.store, deliver=deliver or self.out,
                           log=self.lines.append, chat_fn=model, **kw)

    def job(self, sched="every day at 9am", prompt="Say hi", now=T0, **kw):
        return cron.create(self.store, sched, prompt, now=now, **kw)


class CreateTests(CronBase):
    def test_next_run_and_defaults(self):
        j = self.job()
        self.assertEqual(j["next_run_at"], local_epoch(2026, 10, 28, 9, 0))
        self.assertEqual((j["state"], j["repeat_times"], j["name"]), ("scheduled", None, "Say hi"))
        once = self.job("in 30m", "Tea is ready", name="tea")
        self.assertEqual((once["next_run_at"], once["repeat_times"], once["name"]), (T0 + 1800, 1, "tea"))

    def test_rejects(self):
        with self.assertRaises(cron.CronError):
            self.job(prompt="  ")
        with self.assertRaises(cron.CronError):
            self.job("2026-10-27T09:00")
        with self.assertRaises(schedule.ScheduleError):
            self.job("whenever")
        with self.assertRaises(cron.CronError):
            self.job("0 0 30 2 *")

    def test_resolve_pause_resume_remove(self):
        j = self.job(name="morning")
        self.assertEqual(cron.resolve(self.store, j["id"][:4])["id"], j["id"])
        self.assertEqual(cron.resolve(self.store, "morning")["id"], j["id"])
        self.assertEqual(cron.pause(self.store, "morning")["state"], "paused")
        self.assertEqual(self.store.cron_due(T0 + 86400 * 3), [])
        resumed = cron.resume(self.store, "morning")
        self.assertEqual(resumed["next_run_at"], j["next_run_at"])  # the slot is kept, so it is due now
        self.assertEqual(self.store.cron_due(T0 + 7200), [j["id"]])
        cron.remove(self.store, j["id"])
        with self.assertRaises(cron.CronError):
            cron.resolve(self.store, "morning")


class PlanTests(CronBase):
    def plan(self, job, now):
        return cron.plan(self.store.cron_job(job["id"]), now)

    def test_on_time_late_and_collapsed_catch_up(self):
        j = self.job()
        nine = local_epoch(2026, 10, 28, 9, 0)
        p = self.plan(j, nine + 60)
        self.assertEqual((p.action, p.kind, p.next_run_at), ("fire", "on_time", nine + 86400))
        self.assertEqual(self.plan(j, nine + 1800).kind, "late")
        # three days late: one catch-up run now, then the next real 9am
        p = self.plan(j, nine + 3 * 86400 + 3600 * 5)
        self.assertEqual((p.action, p.kind), ("fire", "catch_up"))
        self.assertEqual(p.next_run_at, local_epoch(2026, 11, 1, 9, 0))

    def test_interval_grace_is_half_the_period(self):
        j = self.job("every 30m")
        slot = j["next_run_at"]
        self.assertEqual(self.plan(j, slot + 899).kind, "late")
        self.assertEqual(self.plan(j, slot + 901).kind, "catch_up")
        self.assertEqual(self.plan(j, slot + 901).next_run_at, slot + 1800)  # still ahead: keep the cadence
        self.assertEqual(self.plan(j, slot + 4000).next_run_at, slot + 4000 + 1800)  # backlog collapsed

    def test_catch_up_off_skips(self):
        j = self.job(catch_up=False)
        p = self.plan(j, local_epoch(2026, 10, 29, 20, 0))
        self.assertEqual((p.action, p.next_run_at), ("skip", local_epoch(2026, 10, 30, 9, 0)))

    def test_one_shot_grace(self):
        j = self.job("in 10m")
        self.assertEqual(self.plan(j, T0 + 600 + 1200).action, "fire")
        p = self.plan(j, T0 + 600 + cron.ONESHOT_GRACE_S + 1)
        self.assertEqual((p.action, p.state), ("retire", "completed"))

    def test_finite_repeat_completes(self):
        j = self.job("every 1h", repeat=2)
        self.store.cron_update(j["id"], repeat_done=1)
        p = self.plan(j, j["next_run_at"])
        self.assertEqual((p.action, p.next_run_at, p.state), ("fire", None, "completed"))


class ClaimTests(CronBase):
    def test_a_slot_is_claimed_once_even_across_connections(self):
        j = self.job()
        slot = j["next_run_at"]
        other = Store(self.s.db_path)
        try:
            first = self.store.cron_claim(j["id"], slot, "on_time", next_run_at=slot + 86400,
                                          state="scheduled", bump=True)
            second = other.cron_claim(j["id"], slot, "on_time", next_run_at=slot + 86400,
                                      state="scheduled", bump=True)
        finally:
            other.close()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.store.cron_job(j["id"])["repeat_done"], 1)

    def test_a_slot_already_in_the_ledger_never_runs_again(self):
        j = self.job()
        slot = j["next_run_at"]
        self.store.cron_claim(j["id"], slot, "on_time", next_run_at=slot + 86400, state="scheduled", bump=True)
        self.store.cron_update(j["id"], next_run_at=slot)  # someone sets it back by hand
        model = FakeModel([reply("should not run")])
        self.assertIsNone(cron.fire(self.store, self.runner(model), j["id"], now=slot + 10))
        self.assertEqual(model.calls, [])
        self.assertGreater(self.store.cron_job(j["id"])["next_run_at"], slot)

    def test_second_tick_does_not_refire(self):
        j = self.job("in 5m")
        model = FakeModel([reply("Tea!")])
        r = self.runner(model)
        self.assertEqual(len(cron.tick(self.store, r, now=T0 + 301)), 1)
        self.assertEqual(cron.tick(self.store, r, now=T0 + 302), [])
        done = self.store.cron_job(j["id"])
        self.assertEqual((done["state"], done["next_run_at"], done["last_status"]), ("completed", None, "ok"))


class RunnerTests(CronBase):
    def fire(self, model, job=None, deliver=None, **kw):
        job = job or self.job("in 5m", "Check the thing")
        return cron.fire(self.store, self.runner(model, deliver, **kw), job["id"], now=T0 + 301)

    def test_reply_is_delivered_from_a_fresh_cron_session(self):
        conv = self.store.new_session()
        model = FakeModel([reply("All good.")])
        res = self.fire(model)
        self.assertEqual((res.status, res.delivery), ("ok", "sent"))
        self.assertEqual(self.out.sent, ["[scheduled] Check the thing\nAll good."])
        run = self.store.cron_run(res.run_id)
        self.assertEqual((run["status"], run["kind"], run["output"]), ("ok", "on_time", "All good."))
        self.assertEqual(self.store.session_field(run["session_id"], "origin"), "cron")
        self.assertEqual(self.store.latest_session(), conv)  # chat resume never lands in a job's run
        sent = model.calls[0]
        self.assertEqual([m["role"] for m in sent], ["system", "user"])
        self.assertIn("scheduled job, running unattended", sent[1]["api_content"])
        self.assertIn("Check the thing", sent[1]["api_content"])

    def test_memory_tool_is_off_in_a_run(self):
        seen = []

        def model(s, key, messages, schemas, **kw):
            seen.append([sch["function"]["name"] for sch in schemas])
            return reply("ok")
        self.fire(model)
        self.assertNotIn("memory", seen[0])
        self.assertIn("run_shell", seen[0])

    def test_silent_sends_nothing(self):
        for text in ("[SILENT]", "Nothing new.\nSILENT", "NO_REPLY"):
            self.out.sent.clear()
            res = self.fire(FakeModel([reply(text)]), job=self.job("in 5m", text))
            self.assertEqual((res.status, res.delivery), ("silent", "silent"), text)
            self.assertEqual(self.out.sent, [])
            self.assertEqual(self.store.cron_run(res.run_id)["status"], "ok")

    def test_failure_marker_is_a_failure_and_counts(self):
        j = self.job("every 1h", "Check the site")
        self.store.cron_update(j["id"], next_run_at=T0 + 300)
        res = self.fire(FakeModel([reply("[CRON_FAILURE]\nThe site is down.")]), job=j)
        self.assertEqual(res.status, "failed")
        self.assertIn("Failed: The site is down.", self.out.sent[0])
        self.assertIn("Next try:", self.out.sent[0])
        after = self.store.cron_job(j["id"])
        self.assertEqual((after["last_status"], after["failure_streak"]), ("failed", 1))

    def test_provider_error_and_crash_both_report(self):
        res = self.fire(FakeModel([provider.ProviderError("HTTP 401: bad key")]))
        self.assertEqual(res.status, "failed")
        self.assertIn("HTTP 401: bad key", self.out.sent[-1])
        res = self.fire(FakeModel([RuntimeError("boom")]), job=self.job("in 5m", "crashy"))
        self.assertEqual(res.status, "failed")
        self.assertIn("crashed: RuntimeError: boom", self.out.sent[-1])
        self.assertEqual(self.store.cron_run(res.run_id)["status"], "failed")

    def test_delivery_failure_is_not_a_job_failure(self):
        res = self.fire(FakeModel([reply("Done.")]), deliver=Recorder(fail=True))
        self.assertEqual((res.status, res.delivery), ("ok", "failed"))
        job = self.store.cron_job(res.job_id)
        self.assertEqual((job["last_status"], job["failure_streak"]), ("ok", 0))

    def test_risky_tool_denied_without_an_always_grant(self):
        call = tool_call("run_shell", {"command": "printf hi", "purpose": "say hi"})
        model = FakeModel([{"role": "assistant", "content": None, "tool_calls": [call]}, reply("Couldn't run it.")])
        res = self.fire(model)
        self.assertEqual(len(res.denied), 1)
        self.assertIn("Denied, because nobody was there to approve", self.out.sent[0])
        self.assertTrue(model.calls[1][-1]["content"].startswith("denied:"))

    @unittest.skipUnless(os.name == "posix", "run_shell needs POSIX pipes")
    def test_always_grant_lets_the_exact_action_run(self):
        args = {"command": "printf hi", "purpose": "say hi"}
        self.store.grant_tool(tools.action_key(self.s, "run_shell", args), 0, "run_shell")
        model = FakeModel([{"role": "assistant", "content": None, "tool_calls": [tool_call("run_shell", args)]},
                           reply("It said hi.")])
        res = self.fire(model)
        self.assertEqual(res.denied, [])
        self.assertIn("exit code 0", model.calls[1][-1]["content"])
        self.assertNotIn("Denied", self.out.sent[0])

    def test_session_grant_from_a_conversation_does_not_carry_over(self):
        conv = self.store.new_session()
        self.store.grant_tool("*", conv, "ordinary tools")
        call = tool_call("run_shell", {"command": "printf hi"})
        res = self.fire(FakeModel([{"role": "assistant", "content": None, "tool_calls": [call]}, reply("no")]))
        self.assertEqual(len(res.denied), 1)

    def test_run_time_limit(self):
        ticks = iter([0.0] + [cron.RUN_LIMIT_S + 1] * 50)
        call = tool_call("list_dir", {"path": "."})
        model = FakeModel([{"role": "assistant", "content": None, "tool_calls": [call]}, reply("late")])
        res = self.fire(model, clock=lambda: next(ticks))
        self.assertEqual(res.status, "failed")
        self.assertIn("longer than its 15-minute limit", res.error)

    def test_missed_one_shot_is_reported_not_run(self):
        j = self.job("in 5m", "Tea")
        model = FakeModel([reply("should not run")])
        res = cron.fire(self.store, self.runner(model), j["id"], now=T0 + 300 + cron.ONESHOT_GRACE_S + 60)
        self.assertEqual(res.status, "missed")
        self.assertEqual(model.calls, [])
        self.assertIn("Missed and not run", self.out.sent[0])
        self.assertEqual(self.store.cron_job(j["id"])["state"], "completed")

    def test_late_run_says_so(self):
        j = self.job("every day at 9am", "Morning")
        res = cron.fire(self.store, self.runner(FakeModel([reply("Hi")])), j["id"],
                        now=local_epoch(2026, 10, 30, 7, 0))
        self.assertEqual(self.store.cron_run(res.run_id)["kind"], "catch_up")
        self.assertIn("ran late", self.out.sent[0])

    def test_manual_run_leaves_the_schedule_alone(self):
        j = self.job()
        res = cron.run_now(self.store, self.runner(FakeModel([reply("Now.")])), j["id"])
        self.assertEqual(res.status, "ok")
        self.assertEqual(self.store.cron_job(j["id"])["next_run_at"], j["next_run_at"])
        self.assertEqual(self.store.cron_run(res.run_id)["kind"], "manual")


class TickTests(CronBase):
    def test_bad_job_does_not_stop_the_scan(self):
        good = self.job("in 5m", "good")
        with self.store.db:
            self.store.db.execute("INSERT INTO cron_jobs (id, name, prompt, schedule, state, next_run_at, created) "
                                  "VALUES ('bad', 'bad', 'x', 'not json', 'scheduled', ?, ?)", (T0, T0))
        results = cron.tick(self.store, self.runner(FakeModel([reply("fine")])), now=T0 + 301)
        self.assertEqual([r.job_id for r in results], [good["id"]])
        self.assertEqual(self.store.cron_job("bad")["state"], "error")

    def test_recover_closes_runs_a_dead_process_left_open(self):
        j = self.job()
        run_id = self.store.cron_claim(j["id"], j["next_run_at"], "on_time", next_run_at=j["next_run_at"] + 86400,
                                       state="scheduled", bump=True)
        with self.store.db:
            self.store.db.execute("UPDATE cron_runs SET pid = 999999 WHERE id = ?", (run_id,))
        with patch("chelus.store._pid_alive", return_value=False):
            orphans = cron.recover(self.store, self.runner(FakeModel([])))
        self.assertEqual([r["id"] for r in orphans], [run_id])
        self.assertEqual(self.store.cron_run(run_id)["status"], "unknown")
        self.assertIn("may or may not have finished", self.out.sent[0])

    def test_prune_keeps_recent_runs_and_drops_old_finished_jobs(self):
        j = self.job("in 5m")
        cron.tick(self.store, self.runner(FakeModel([reply("x")])), now=T0 + 301)
        self.store.cron_update(j["id"], finished_at=time.time() - cron.KEEP_DONE_S - 10)
        cron.tick(self.store, self.runner(FakeModel([])), now=T0 + 400)
        self.assertIsNone(self.store.cron_job(j["id"]))
        self.assertEqual(len(self.store.cron_runs(j["id"])), 1)


class BridgeTests(CronBase):
    def test_poll_wait_shortens_for_a_due_job_and_tick_runs_it(self):
        from chelus import telegram

        class Bot:
            def __init__(self):
                self.sent = []

            def call(self, method, params=None, wait=0):
                if method == "sendMessage":
                    self.sent.append(params["text"])
                return []

        bot = Bot()
        factory = type("Factory", (), {"store": self.store})()
        bridge = telegram.Bridge(self.s, bot, 42, agent_factory=factory, log=self.lines.append)
        self.assertEqual(bridge.poll_wait(), telegram.POLL_SECONDS)
        bridge.cron = cron.Runner(self.s, "k", self.store, deliver=lambda text: bridge._send(42, text),
                                  log=self.lines.append, chat_fn=FakeModel([reply("Ding")]))
        cron.create(self.store, "in 10m", "ding", name="ding")
        self.assertEqual(bridge.poll_wait(), telegram.POLL_SECONDS)
        self.store.cron_update(cron.resolve(self.store, "ding")["id"], next_run_at=time.time() + 10.2)
        self.assertEqual(bridge.poll_wait(), 11)
        self.store.cron_update(cron.resolve(self.store, "ding")["id"], next_run_at=time.time() - 1)
        self.assertEqual(bridge.poll_wait(), 1)
        with patch("chelus.curator.maybe_run", return_value=None):
            bridge.housekeeping()
        self.assertTrue(bot.sent and bot.sent[0].startswith("[scheduled] ding\nDing"))


if __name__ == "__main__":
    unittest.main()
