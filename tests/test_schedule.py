"""Schedule parsing and next-run tests. DST cases need time.tzset (POSIX; run on the phone)."""
import os
import time
import unittest

from chelus import schedule as sc
from chelus.schedule import ScheduleError, local_epoch


def wall(t):
    lt = time.localtime(t)
    return (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min)


class ParseTests(unittest.TestCase):
    NOW = local_epoch(2026, 10, 28, 14, 7)  # a Wednesday

    def p(self, text):
        return sc.parse(text, now=self.NOW)

    def test_intervals(self):
        self.assertEqual(self.p("30m"), {"kind": "interval", "minutes": 30, "display": "every 30m"})
        self.assertEqual(self.p("every 2h")["minutes"], 120)
        self.assertEqual(self.p("every hour")["minutes"], 60)
        self.assertEqual(self.p("every day")["minutes"], 1440)
        self.assertEqual(self.p("90 minutes")["display"], "every 90m")
        self.assertEqual(self.p("every 1d")["display"], "every 1d")

    def test_one_shot_by_duration(self):
        s = self.p("in 30m")
        self.assertEqual((s["kind"], s["run_at"], s["display"]), ("once", self.NOW + 1800, "once in 30m"))
        self.assertEqual(self.p("in 2 hours")["run_at"], self.NOW + 7200)

    def test_natural_phrases(self):
        self.assertEqual(self.p("every monday 9am")["expr"], "0 9 * * 1")
        self.assertEqual(self.p("weekdays at 9am")["expr"], "0 9 * * 1-5")
        self.assertEqual(self.p("every day at 21:30")["expr"], "30 21 * * *")
        self.assertEqual(self.p("mon, wed and fri at 7:15pm")["expr"], "15 19 * * 1,3,5")
        self.assertEqual(self.p("every weekend at noon")["expr"], "0 12 * * 0,6")
        self.assertEqual(self.p("every monday 9am")["display"], "every monday 9am")

    def test_cron_and_names(self):
        self.assertEqual(self.p("0 9 * * MON-FRI")["expr"], "0 9 * * MON-FRI")
        self.assertEqual(self.p("*/15 * * * *")["kind"], "cron")

    def test_timestamps(self):
        s = self.p("2026-11-02T09:00")
        self.assertEqual(s["kind"], "once")
        self.assertEqual(wall(s["run_at"]), (2026, 11, 2, 9, 0))
        self.assertEqual(self.p("2026-11-02T09:00:00+00:00")["run_at"], 1793610000.0)
        self.assertEqual(self.p("2026-11-02T09:00Z")["run_at"], 1793610000.0)

    def test_rejects(self):
        for bad in ("", "every monday", "tomorrow", "7", "every 0m", "0 9 * *", "61 * * * *",
                    "0 9 * * 8", "5-1 * * * *", "*/0 * * * *", "0 9 31 2 1-", "at noon", "every banana"):
            with self.subTest(bad=bad), self.assertRaises(ScheduleError):
                self.p(bad)


class CronTests(unittest.TestCase):
    def nxt(self, expr, *base):
        return wall(sc.CronExpr(expr).next_after(local_epoch(*base)))

    def test_strictly_after(self):
        self.assertEqual(self.nxt("0 9 * * *", 2026, 10, 28, 9, 0), (2026, 10, 29, 9, 0))
        self.assertEqual(self.nxt("0 9 * * *", 2026, 10, 28, 8, 59), (2026, 10, 28, 9, 0))
        self.assertEqual(self.nxt("*/15 * * * *", 2026, 10, 28, 23, 50), (2026, 10, 29, 0, 0))

    def test_seconds_inside_a_minute(self):
        base = local_epoch(2026, 10, 28, 9, 0) + 30
        self.assertEqual(wall(sc.CronExpr("* * * * *").next_after(base)), (2026, 10, 28, 9, 1))

    def test_weekdays_names_and_sunday_as_7(self):
        # 2026-10-31 is a Saturday
        self.assertEqual(self.nxt("0 9 * * 1-5", 2026, 10, 30, 10, 0), (2026, 11, 2, 9, 0))
        self.assertEqual(self.nxt("0 9 * * sun", 2026, 10, 30, 10, 0), (2026, 11, 1, 9, 0))
        self.assertEqual(self.nxt("0 9 * * 7", 2026, 10, 30, 10, 0), (2026, 11, 1, 9, 0))
        self.assertEqual(self.nxt("0 0 1 jan *", 2026, 10, 30, 10, 0), (2027, 1, 1, 0, 0))

    def test_dom_or_dow_when_both_restricted(self):
        # the 15th, or any Monday: from Wed Oct 28 the next is Mon Nov 2
        self.assertEqual(self.nxt("0 9 15 * 1", 2026, 10, 28, 10, 0), (2026, 11, 2, 9, 0))
        # a */ field counts as unrestricted: only the 15th qualifies, on any day
        self.assertEqual(self.nxt("0 9 15 * */1", 2026, 10, 28, 10, 0), (2026, 11, 15, 9, 0))

    def test_leap_day_and_never(self):
        self.assertEqual(self.nxt("0 0 29 2 *", 2026, 10, 28, 0, 0), (2028, 2, 29, 0, 0))
        self.assertIsNone(sc.CronExpr("0 0 30 2 *").next_after(local_epoch(2026, 1, 1, 0, 0)))

    def test_period_and_next_run(self):
        base = local_epoch(2026, 10, 28, 10, 0)
        self.assertEqual(sc.period_seconds(sc.parse("every 2h"), base), 7200)
        self.assertEqual(sc.period_seconds(sc.parse("0 9 * * *"), base), 86400)
        self.assertEqual(sc.next_run(sc.parse("every 2h"), base), base + 7200)
        once = sc.parse("in 5m", now=base)
        self.assertEqual(sc.next_run(once, base), base + 300)
        self.assertIsNone(sc.next_run(once, base + 300))


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset (POSIX)")
class DstTests(unittest.TestCase):
    """US Eastern rules as a POSIX TZ string, so no tz database is needed. 2026: clocks go
    forward Mar 8 at 2:00, back Nov 1 at 2:00."""

    def setUp(self):
        self.old = os.environ.get("TZ")
        os.environ["TZ"] = "EST5EDT,M3.2.0,M11.1.0"
        time.tzset()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        time.tzset()

    def test_daily_job_runs_once_when_clocks_go_back(self):
        expr = sc.CronExpr("30 1 * * *")
        first = expr.next_after(local_epoch(2026, 10, 31, 12, 0))
        self.assertEqual(wall(first), (2026, 11, 1, 1, 30))
        self.assertEqual(time.localtime(first).tm_isdst, 1)  # the earlier of the two 1:30s
        second = expr.next_after(first)
        self.assertEqual(wall(second), (2026, 11, 2, 1, 30))

    def test_base_in_the_repeated_hour_never_goes_backwards(self):
        base = local_epoch(2026, 11, 1, 1, 45) + 3600  # 1:45 EST, the second time round
        self.assertEqual(time.localtime(base).tm_isdst, 0)
        nxt = sc.CronExpr("50 * * * *").next_after(base)
        self.assertGreater(nxt, base)
        self.assertEqual(wall(nxt), (2026, 11, 1, 1, 50))

    def test_time_in_the_spring_gap_runs_after_it(self):
        nxt = sc.CronExpr("30 2 * * *").next_after(local_epoch(2026, 3, 7, 12, 0))
        self.assertEqual(wall(nxt), (2026, 3, 8, 3, 30))
        self.assertEqual(wall(sc.CronExpr("30 2 * * *").next_after(nxt)), (2026, 3, 9, 2, 30))

    def test_interval_counts_real_time_across_the_change(self):
        base = local_epoch(2026, 11, 1, 0, 30)
        self.assertEqual(sc.next_run(sc.parse("every 2h"), base) - base, 7200)
        self.assertEqual(wall(base + 7200), (2026, 11, 1, 1, 30))  # 1:30 EST: real two hours

    def test_local_timestamp_in_repeated_hour_is_the_earlier(self):
        t = sc.parse("2026-11-01T01:30")["run_at"]
        self.assertEqual(time.localtime(t).tm_isdst, 1)


if __name__ == "__main__":
    unittest.main()
