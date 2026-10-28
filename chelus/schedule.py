"""Schedules for cron jobs: parse what the owner or the model wrote, and find the next run.

The forms follow Hermes Agent's cron (cron/jobs.py parse_schedule; docs/hermes/cron.md):

- "30m", "every 2h", "every hour": a recurring interval
- "in 30m", "in 2 hours": once, that long from now
- "every monday 9am", "weekdays at 9am", "every day at 21:30": recurring, compiled to cron
- "0 9 * * 1-5": five-field cron, with day and month names
- "2026-11-02T09:00": once at that time; a timestamp without an offset is phone-local

Times are epoch seconds. Wall-clock times are phone-local through time.localtime and
time.mktime, which follow Android's timezone setting and its DST rules. Python's zoneinfo
has no data in Termux, so IANA zone names are not accepted.

Hermes uses croniter; this is a small stdlib matcher with the classic rules: lists, ranges
and steps; names; 7 is also Sunday; and when both day-of-month and day-of-week are
restricted, a day matching either one counts (Vixie cron).
"""

from __future__ import annotations

import datetime
import re
import time
from typing import Any

ONCE, INTERVAL, CRON = "once", "interval", "cron"
SEARCH_DAYS = 366 * 8  # a cron expression with no match in 8 years never matches (Feb 30)


class ScheduleError(ValueError):
    """A schedule that can't be parsed; the message says what forms work."""


HELP = ("Use: '30m' or 'every 2h' (recurring), 'in 30m' (once), 'every monday 9am' or "
        "'weekdays at 9am' (recurring), cron like '0 9 * * *', or a timestamp like "
        "'2026-11-02T09:00' (once, phone-local time).")

_UNITS = {"m": 1, "h": 60, "d": 1440}
_DURATION = re.compile(r"^(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$")
_DAYS = {"sun": 0, "sunday": 0, "mon": 1, "monday": 1, "tue": 2, "tues": 2, "tuesday": 2,
         "wed": 3, "weds": 3, "wednesday": 3, "thu": 4, "thur": 4, "thurs": 4, "thursday": 4,
         "fri": 5, "friday": 5, "sat": 6, "saturday": 6}
_DAY_SPECS = {"day": "*", "daily": "*", "everyday": "*", "weekday": "1-5", "weekdays": "1-5",
              "weekend": "0,6", "weekends": "0,6"}
_MONTH_NAMES = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DOW_NAMES = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
# (low, high, names) per field: minute, hour, day of month, month, day of week
_FIELDS = ((0, 59, {}), (0, 23, {}), (1, 31, {}), (1, 12, _MONTH_NAMES), (0, 7, _DOW_NAMES))


def parse_duration(text: str) -> int:
    """Minutes in "30m", "2 hours", "1d"; a bare unit ("hour") is one of it."""
    m = _DURATION.match(text.strip().lower())
    if not m:
        raise ScheduleError(f"bad duration {text!r}: use something like '30m', '2h' or '1d'")
    minutes = (int(m.group(1)) if m.group(1) else 1) * _UNITS[m.group(2)[0]]
    if minutes < 1:
        raise ScheduleError("a duration must be at least one minute")
    return minutes


def _clock(text: str) -> tuple[int, int] | None:
    """(hour, minute) for 9am, 9:30pm, 21:30, 7, noon, midnight."""
    t = text.strip().lower().replace(" ", "")
    if t in ("noon", "midday"):
        return 12, 0
    if t == "midnight":
        return 0, 0
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", t)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if m.group(3) == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _natural_to_cron(rest: str) -> str | None:
    """'monday 9am', 'weekdays at 9am', 'mon, wed and fri at 7:30' -> cron, or None."""
    tokens = rest.lower().replace(",", " ").split()
    if not tokens:
        return None
    dow = _DAY_SPECS.get(tokens[0])
    idx = 1
    if dow is None:
        days: list[int] = []
        idx = len(tokens)
        for i, tok in enumerate(tokens):
            if tok == "and":
                continue
            if tok not in _DAYS:
                idx = i
                break
            if _DAYS[tok] not in days:
                days.append(_DAYS[tok])
        if not days:
            return None
        dow = ",".join(str(d) for d in days)
    when = tokens[idx:]
    if when and when[0] == "at":
        when = when[1:]
    if not when:
        return None
    clock = _clock(" ".join(when))
    if clock is None:
        return None
    return f"{clock[1]} {clock[0]} * * {dow}"


def parse(text: str, now: float | None = None) -> dict[str, Any]:
    """A schedule dict: {"kind", "display"} plus "run_at" (once), "minutes" (interval) or
    "expr" (cron). Raises ScheduleError."""
    now = time.time() if now is None else now
    original = (text or "").strip()
    lower = original.lower()
    if not original:
        raise ScheduleError("a schedule is required. " + HELP)

    every = lower.startswith("every ")
    rest = lower[6:].strip() if every else lower
    expr = _natural_to_cron(rest)
    if expr is not None:
        CronExpr(expr)
        return {"kind": CRON, "expr": expr, "display": original}
    if every:
        try:
            return _interval(parse_duration(rest))
        except ScheduleError:
            raise ScheduleError(f"can't read {original!r}. " + HELP) from None

    parts = original.split()
    if len(parts) == 5 and all(re.match(r"^[A-Za-z\d*,/-]+$", p) for p in parts):
        CronExpr(original)  # raises with the reason
        return {"kind": CRON, "expr": original, "display": original}

    if "T" in original or re.match(r"^\d{4}-\d{2}-\d{2}", original):
        try:
            dt = datetime.datetime.fromisoformat(original)
        except ValueError as e:
            raise ScheduleError(f"bad timestamp {original!r} ({e}). " + HELP) from None
        run_at = dt.timestamp() if dt.tzinfo else local_epoch(
            dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        return {"kind": ONCE, "run_at": run_at, "display": "once at " + time.strftime("%Y-%m-%d %H:%M", time.localtime(run_at))}

    if lower.startswith("in "):
        minutes = parse_duration(lower[3:])
        return {"kind": ONCE, "run_at": now + minutes * 60, "display": f"once in {original[3:].strip()}"}

    try:
        return _interval(parse_duration(lower))
    except ScheduleError:
        raise ScheduleError(f"can't read {original!r}. " + HELP) from None


def _interval(minutes: int) -> dict[str, Any]:
    if minutes % 1440 == 0:
        label = f"{minutes // 1440}d"
    elif minutes % 60 == 0:
        label = f"{minutes // 60}h"
    else:
        label = f"{minutes}m"
    return {"kind": INTERVAL, "minutes": minutes, "display": f"every {label}"}


def recurring(sched: dict[str, Any]) -> bool:
    return sched.get("kind") in (INTERVAL, CRON)


def next_run(sched: dict[str, Any], after: float) -> float | None:
    """The first run strictly after `after` for a recurring schedule; for a one-shot, its
    time if that is still ahead. None when there is none."""
    kind = sched.get("kind")
    if kind == ONCE:
        run_at = float(sched["run_at"])
        return run_at if run_at > after else None
    if kind == INTERVAL:
        return after + int(sched["minutes"]) * 60
    if kind == CRON:
        return CronExpr(sched["expr"]).next_after(after)
    return None


def period_seconds(sched: dict[str, Any], around: float) -> float | None:
    """How far apart runs are, for the catch-up window: the interval, or the gap between a
    cron schedule's next two runs."""
    kind = sched.get("kind")
    if kind == INTERVAL:
        return int(sched["minutes"]) * 60.0
    if kind == CRON:
        first = next_run(sched, around)
        second = next_run(sched, first) if first is not None else None
        if first is not None and second is not None:
            return second - first
    return None


def local_epoch(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> float:
    """The instant a phone-local wall time names. In the autumn's repeated hour that is the
    earlier one; a time inside the spring gap means the instant just after the gap."""
    found = _wall_instants(year, month, day, hour, minute, second)
    return min(found)


def _wall_instants(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> list[float]:
    """Every instant that reads as this wall time (two in a repeated hour). For a time that
    doesn't exist (the spring gap), the later normalization, which lands after the gap."""
    want = (year, month, day, hour, minute, second)
    found, raw = [], []
    for dst in (0, 1):
        e = time.mktime((year, month, day, hour, minute, second, 0, 0, dst))
        raw.append(e)
        lt = time.localtime(e)
        if (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec) == want and e not in found:
            found.append(e)
    return found or [max(raw)]


class CronExpr:
    """A five-field cron expression, matched against phone-local wall time."""

    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ScheduleError(f"cron needs 5 fields (minute hour day month weekday), got {len(parts)} in {expr!r}")
        sets = []
        for text, (low, high, names) in zip(parts, _FIELDS):
            sets.append(_field(text, low, high, names, expr))
        self.minutes, self.hours, self.days, self.months, dows = sets
        self.dows = {d % 7 for d in dows}
        # Vixie cron: a field starting with "*" (also "*/2") counts as unrestricted here
        self.dom_any = parts[2].startswith("*")
        self.dow_any = parts[4].startswith("*")
        self.expr = expr

    def day_matches(self, d: datetime.date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.weekday() + 1) % 7 in self.dows  # cron: 0 = Sunday
        if not self.dom_any and not self.dow_any:
            return dom or dow
        return dom and dow

    def next_after(self, after: float) -> float | None:
        """The first matching minute strictly after `after`. Candidates are walked in wall
        time after the base's wall time, so a daily job runs once on the day the clocks go
        back; each candidate takes its earliest instant after `after` (Hermes's fold rule)."""
        lt = time.localtime(after)
        base_day = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
        base_hm = (lt.tm_hour, lt.tm_min)
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for offset in range(SEARCH_DAYS):
            day = base_day + datetime.timedelta(days=offset)
            if not self.day_matches(day):
                continue
            for h in hours:
                if offset == 0 and h < base_hm[0]:
                    continue
                for m in minutes:
                    if offset == 0 and (h, m) <= base_hm:
                        continue
                    later = [e for e in _wall_instants(day.year, day.month, day.day, h, m) if e > after]
                    if later:
                        return min(later)
        return None


def _field(text: str, low: int, high: int, names: dict[str, int], expr: str) -> set[int]:
    def value(tok: str) -> int:
        tok = tok.lower()
        if tok in names:
            return names[tok]
        if not tok.isdigit():
            raise ScheduleError(f"bad value {tok!r} in cron {expr!r}")
        n = int(tok)
        if not low <= n <= high:
            raise ScheduleError(f"{n} is out of range {low}-{high} in cron {expr!r}")
        return n

    out: set[int] = set()
    for item in text.split(","):
        if not item:
            raise ScheduleError(f"empty list item in cron {expr!r}")
        rng, _, step_text = item.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"bad step {step_text!r} in cron {expr!r}")
            step = int(step_text)
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start, end = value(a), value(b)
            if start > end:
                raise ScheduleError(f"backwards range {rng!r} in cron {expr!r}")
        else:
            start = value(rng)
            end = high if step_text else start
        out.update(range(start, end + 1, step))
    return out


def describe(when: float | None) -> str:
    if when is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(when))
