"""Is a cut-off reply stuck in a repetition loop?

A model in a loop can spend its whole output budget echoing one fragment. Asking it to
"continue" would stitch the loop into the reply, so the continuation path checks this first.

Same rules and thresholds as Hermes Agent's repetition_guard.is_repetition_dominated
(Nous Research, MIT): only long verbatim repeats covering at least half the text count,
and anything under 400 characters passes.
"""

from __future__ import annotations

import math
from collections import Counter

MIN_CHARS = 400      # shorter fragments legitimately repeat tokens
WINDOW = 60          # an exact repeat this long is far beyond normal phrasing reuse
MIN_REPEATS = 5      # a unit must recur at least this often
DOMINANCE = 0.5      # repeated material must cover at least this share of the text
_ANCHORS = 32        # anchors sampled by the periodic scan (keeps it linear)
_MATCHES_PER_ANCHOR = 8


def is_repetition_dominated(text: str) -> bool:
    if not isinstance(text, str) or len(text) < MIN_CHARS:
        return False
    n = len(text)
    return _one_line_dominates(text, n) or _window_dominates(text, n) or _periodic_run_dominates(text, n)


def _one_line_dominates(text: str, n: int) -> bool:
    """The common echo: one line repeated until it fills half the text."""
    counts = Counter(line.strip() for line in text.splitlines() if line.strip())
    return any(c >= MIN_REPEATS and c * len(line) >= n * DOMINANCE for line, c in counts.items())


def _window_dominates(text: str, n: int) -> bool:
    """One 60-char window recurs often enough to cover half the text (catches loops whose
    copies differ by a counter or a stray token)."""
    needed = max(MIN_REPEATS, math.ceil(n * DOMINANCE / WINDOW))
    seen: dict[str, int] = {}
    for i in range(n - WINDOW + 1):
        key = text[i:i + WINDOW]
        c = seen.get(key, 0) + 1
        if c >= needed:
            return True
        seen[key] = c
    return False


def _periodic_run_dominates(text: str, n: int) -> bool:
    """A long unit (a paragraph, a code block) repeated exactly. Each of its 60-char windows
    recurs too rarely for the window count, so find a period from a sampled anchor and
    measure the whole run where text[i] == text[i + period]."""
    last = n - WINDOW
    if last < 1:
        return False
    step = max(1, -(-last // (_ANCHORS - 1)))
    starts = list(range(0, last + 1, step))
    if starts[-1] != last:
        starts.append(last)
    rejected: list[tuple[int, int, int]] = []  # runs already measured, so later anchors skip them
    for start in starts:
        anchor = text[start:start + WINDOW]
        frm = start + 1
        for _ in range(_MATCHES_PER_ANCHOR):
            hit = text.find(anchor, frm)
            if hit < 0:
                break
            period, frm = hit - start, hit + 1
            if any(lo <= start < hi and period % p == 0 for lo, hi, p in rejected):
                continue
            left = start
            while left > 0 and text[left - 1] == text[left - 1 + period]:
                left -= 1
            right = start + WINDOW
            while right + period < n and text[right] == text[right + period]:
                right += 1
            length = right + period - left
            if length >= MIN_REPEATS * period and length >= n * DOMINANCE:
                return True
            rejected.append((left, right + period, period))
    return False
