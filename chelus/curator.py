"""The curator: weekly housekeeping for skills the background review created.

Rebuilt from the deterministic pass of Hermes Agent's curator.py (Nous Research, MIT).
No daemon: whoever is running (the Telegram bridge's poll loop) asks `maybe_run`, which
works only when a week has passed since the last run and Chelus has been idle for 2 hours.
The very first check only starts the clock, so a fresh install never touches the library.

For each skill the background review created (created_by = agent), not pinned, the last
activity is the newest of created, used, viewed and patched:
- idle more than 14 days: marked stale;
- idle more than 30 days: archived (moved to .archive, never deleted; `undo` restores it);
- a stale skill that gets used again is active again (store.usage_bump).
Skills the owner made, by hand or with /learn, are never touched. Hermes's optional LLM
consolidation pass is left out, as it is off by default there.
"""

from __future__ import annotations

import time
from typing import Any

from . import skills

INTERVAL_S = 7 * 24 * 3600
MIN_IDLE_S = 2 * 3600
STALE_AFTER_S = 14 * 24 * 3600
ARCHIVE_AFTER_S = 30 * 24 * 3600


def maybe_run(s: Any, store: Any, idle_s: float, now: float | None = None) -> str | None:
    """Run the pass if it's due. Returns a report when something ran, else None."""
    if not s.curator:
        return None
    now = time.time() if now is None else now
    last = store.meta("curator_last")
    if last is None:
        store.set_meta("curator_last", str(now))
        return None
    if now - float(last) < INTERVAL_S or idle_s < MIN_IDLE_S:
        return None
    return run(s, store, now)


def run(s: Any, store: Any, now: float | None = None) -> str:
    now = time.time() if now is None else now
    store.set_meta("curator_last", str(now))
    base = skills.root(s)
    stale, archived = [], []
    for row in store.usage_rows():
        if row["created_by"] != "agent" or row["pinned"] or row["state"] == "archived":
            continue
        last = max([x for x in (row["created"], row["last_used"], row["last_viewed"], row["last_patched"]) if x]
                   or [now])
        idle = now - last
        if idle > ARCHIVE_AFTER_S:
            skill = skills.find(base, row["name"])
            if skill is None:
                store.set_usage(row["name"], state="archived", archived_at=now)
                continue
            dest = skills.archive(base, skill)
            store.add_revision(skill.name, skill.category, "curator", "delete", None, str(dest))
            store.set_usage(skill.name, state="archived", archived_at=now)
            archived.append(skill.name)
        elif idle > STALE_AFTER_S and row["state"] != "stale":
            store.set_usage(row["name"], state="stale")
            stale.append(row["name"])
    parts = []
    if stale:
        parts.append("marked stale (unused 14+ days): " + ", ".join(stale))
    if archived:
        parts.append("archived (unused 30+ days, undo restores): " + ", ".join(archived))
    return "Skill curator: " + ("; ".join(parts) if parts else "nothing to do") + "."
