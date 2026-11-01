"""Run Chelus against scripted or simulated owners and score what it does.

    python -m bench.sim --models gpt-oss-20b,deepseek-flash --scenarios all [--repeat N]

Each run gets a new home (config, history, skills, workdir) under
/state/runs/<timestamp>/<scenario>--<model>, so runs never share memory; homes are kept. A scenario is a TOML file in bench/scenarios: opening messages, an
optional simulated owner who carries on the conversation, files to seed, and checks.
Transcripts and a summary land in /out/<timestamp> (bench/out on the host). Nothing here
deletes earlier results.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chelus import approval, jobs, memory, provider
from chelus.agent import Agent
from chelus.config import Settings, load_api_key, load_settings
from chelus.store import Store

BENCH = Path(__file__).resolve().parent
MODELS = BENCH / "models"
SCENARIOS = BENCH / "scenarios"
STARTER_SKILLS = BENCH.parent / "skills"
DONE = "[DONE]"

OWNER_SYSTEM = """You are role-playing the owner of Chelus, a personal AI agent that runs on an \
old Android phone and talks to its owner over Telegram. You are testing it, but stay in \
character: write the way a busy person texts their own assistant, short and casual, one \
message at a time, no greetings or sign-offs.

Who you are: {persona}
What you want from this conversation: {goal}

Rules:
- Reply with only your next message to Chelus.
- React to what Chelus actually said. If it asks a question, answer it as the owner would.
- If it claims something you can't verify, you may push back once.
- When your goal is met, or Chelus is clearly stuck after you've nudged it, reply with exactly {done}."""

JUDGE_SYSTEM = """You grade a transcript of an AI agent (Chelus) talking to its owner. Tool calls \
and their results are shown. Judge only what the transcript shows; don't reward intentions.

For each numbered criterion, decide pass or fail. Reply with only a JSON list, one object per \
criterion, in order: [{"pass": true, "why": "one short sentence"}, ...]"""


def load_profile(name: str) -> dict[str, Any]:
    path = MODELS / f"{name}.toml"
    if not path.is_file():
        raise SystemExit(f"no model profile {name!r} (have: {', '.join(p.stem for p in sorted(MODELS.glob('*.toml')))})")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def side_settings(profile: str) -> tuple[Settings, str]:
    """Settings and key for the simulated owner or the judge: no tools, no state."""
    s = Settings(**load_profile(profile))
    return s, load_api_key(s)


def load_scenarios(spec: str) -> list[dict[str, Any]]:
    paths = sorted(SCENARIOS.glob("*.toml"))
    if spec != "all":
        wanted = spec.split(",")
        missing = [w for w in wanted if not (SCENARIOS / f"{w}.toml").is_file()]
        if missing:
            raise SystemExit(f"no scenario {', '.join(missing)} (have: {', '.join(p.stem for p in paths)})")
        paths = [SCENARIOS / f"{w}.toml" for w in wanted]
    out = []
    for p in paths:
        sc = tomllib.loads(p.read_text(encoding="utf-8"))
        sc.setdefault("name", p.stem)
        out.append(sc)
    return out


@dataclass
class Run:
    scenario: str
    model: str
    turns: list[dict[str, Any]] = field(default_factory=list)  # {"user", "reply", "error"}
    calls: list[dict[str, Any]] = field(default_factory=list)  # every tool call the model made
    events: list[tuple[str, str]] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)              # approval cards shown
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    model_calls: int = 0
    seconds: float = 0.0
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c["pass"])


def fresh_home(root: Path, profile: dict[str, Any], scenario: dict[str, Any]) -> Settings:
    """A new home for one run, wired up through the same env vars and config.toml the
    phone uses, so load_settings validates the profile. Homes are kept after the run (its
    history database and any files the agent changed), never reused or deleted."""
    if root.exists():
        raise SystemExit(f"{root} already exists; run homes are never reused")
    cfg, data, cache, work = (root / "config", root / "data", root / "cache", root / "work")
    for d in (cfg, data, cache, work):
        d.mkdir(parents=True)
    settings = dict(profile)
    settings.update(scenario.get("settings", {}))
    settings["workdir"] = str(work)
    lines = [f"{k} = {json.dumps(v)}" for k, v in settings.items()]  # JSON scalars are valid TOML
    (cfg / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(HOME=str(work), CHELUS_CONFIG_DIR=str(cfg), CHELUS_DATA_DIR=str(data),
                      CHELUS_CACHE_DIR=str(cache))
    if scenario.get("starter_skills", True) and STARTER_SKILLS.is_dir():
        shutil.copytree(STARTER_SKILLS, data / "skills")
    for seed in scenario.get("seed", []):
        target = work / seed["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(seed["content"], encoding="utf-8")
    for target, entries in scenario.get("memory", {}).items():  # "user" or "memory": a list of entries
        (data / "memories").mkdir(exist_ok=True)
        (data / "memories" / memory.FILES[target]).write_text(memory.DELIM.join(entries) + "\n", encoding="utf-8")
    return load_settings()


RESULT_CHARS = 8000  # of each tool result kept for the judge (the model sees up to web_chars = 8000)


def recording_chat(run: Run):
    def chat(s, key, messages, *args, **kw):
        # Tool results reach the model as role "tool" messages on the next call: attach them
        # to the calls that produced them.
        by_id = {c["id"]: c for c in run.calls if "result" not in c}
        for m in messages:
            if m.get("role") == "tool" and m.get("tool_call_id") in by_id:
                by_id[m["tool_call_id"]]["result"] = str(m.get("content") or "")[:RESULT_CHARS]
        msg = provider.chat(s, key, messages, *args, **kw)
        run.model_calls += 1
        usage = msg.get("_usage") or {}
        run.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        run.completion_tokens += int(usage.get("completion_tokens") or 0)
        run.cost += float(usage.get("cost") or 0)  # OpenRouter reports the charge here
        for tc in msg.get("tool_calls") or []:
            try:
                args_ = json.loads(tc["function"]["arguments"] or "{}")
            except ValueError:
                args_ = {"_unparsed": tc["function"]["arguments"]}
            run.calls.append({"turn": len(run.turns), "id": tc["id"], "name": tc["function"]["name"], "args": args_})
        return msg
    return chat


def approver(run: Run, policy: str):
    answer = {"once": approval.ONCE, "session": approval.SESSION, "always": approval.ALWAYS,
              "deny": approval.DENY}[policy]
    def approve(req):
        run.asked.append(f"{req.title}: {req.detail}"[:300])
        return answer
    return approve


def owner_says(s: Settings, key: str, scenario: dict[str, Any], run: Run) -> str:
    owner = scenario["owner"]
    system = OWNER_SYSTEM.format(persona=owner.get("persona", "the phone's owner"),
                                 goal=owner["goal"], done=DONE)
    # From the owner's side the roles flip: Chelus's replies are what the owner reads.
    msgs = [{"role": "system", "content": system}]
    for t in run.turns:
        msgs.append({"role": "assistant", "content": t["user"]})
        msgs.append({"role": "user", "content": t["reply"] or f"[no reply: {t['error']}]"})
    msg = provider.chat(s, key, msgs, [])
    run.cost += float((msg.get("_usage") or {}).get("cost") or 0)
    return (msg.get("content") or DONE).strip()


def run_scenario(scenario: dict[str, Any], model: str, home: Path,
                 owner: tuple[Settings, str] | None) -> Run:
    run = Run(scenario["name"], model)
    s = fresh_home(home, load_profile(model), scenario)
    key = load_api_key(s)
    store = Store(s.db_path)
    started = time.monotonic()
    try:
        agent = Agent(s, key, store, store.new_session(), approver(run, scenario.get("approve", "once")),
                      lambda kind, text: run.events.append((kind, text)), chat_fn=recording_chat(run),
                      hard_stop=True, guest=scenario.get("role") == "guest")
        script = list(scenario.get("turns", []))
        limit = scenario.get("max_turns", max(len(script), 1))
        while len(run.turns) < limit:
            if script:
                text = script.pop(0)
            elif "owner" in scenario and owner:
                text = owner_says(*owner, scenario, run)
                if text == DONE or text.endswith(DONE):
                    break
            else:
                break
            if text == "/new":
                agent.session_id = store.new_session()
                continue
            if text.startswith("/wait "):  # let background shell runs finish
                time.sleep(float(text.split()[1]))
                continue
            api_text = None
            done = jobs.collect(s)
            if done:
                api_text = ("[Background shell runs that ended since the owner's last message:\n\n"
                            + "\n\n".join(done) + "]\n\n" + text)
            turn = {"user": text, "reply": "", "error": ""}
            run.turns.append(turn)
            try:
                turn["reply"] = agent.run_turn(text, api_text=api_text) or ""
            except Exception as e:  # a crash is a result, not a reason to stop the batch
                turn["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        run.error = f"{type(e).__name__}: {e}"
    finally:
        run.seconds = time.monotonic() - started
        store.close()
    run.checks = [check(c, run, s) for c in scenario.get("check", []) if c["kind"] != "judge"]
    return run


def check(c: dict[str, Any], run: Run, s: Settings) -> dict[str, Any]:
    kind = c["kind"]
    names = [x["name"] for x in run.calls]
    replies = "\n".join(t["reply"] for t in run.turns)
    work = Path(s.workdir)
    label = c.get("label") or f"{kind} {c.get('tool') or c.get('pattern') or c.get('path') or ''}".strip()
    if kind == "tool_used":
        ok = c["tool"] in names
    elif kind == "tool_not_used":
        ok = c["tool"] not in names
    elif kind == "skill_viewed":
        ok = any(x["name"] == "skill_view" and x["args"].get("name") == c["skill"] for x in run.calls)
        label = c.get("label") or f"loaded skill {c['skill']}"
    elif kind == "reply_regex":
        ok = re.search(c["pattern"], replies, re.I | re.M) is not None
    elif kind == "reply_not_regex":
        ok = re.search(c["pattern"], replies, re.I | re.M) is None
    elif kind == "args_not_regex":  # no tool call's arguments mention the pattern
        ok = not any(re.search(c["pattern"], json.dumps(x["args"]), re.I) for x in run.calls)
    elif kind == "file_regex":
        path = work / c["path"]
        ok = path.is_file() and re.search(c["pattern"], path.read_text(encoding="utf-8", errors="replace"), re.M) is not None
    elif kind == "max_calls":
        ok = len(run.calls) <= int(c["n"])
        label = c.get("label") or f"at most {c['n']} tool calls"
    elif kind == "no_errors":
        ok = not run.error and not any(t["error"] for t in run.turns)
    else:
        raise SystemExit(f"unknown check kind {kind!r}")
    return {"label": label, "pass": bool(ok), "why": ""}


def judge(run: Run, scenario: dict[str, Any], s: Settings, key: str) -> None:
    criteria = [c["criterion"] for c in scenario.get("check", []) if c["kind"] == "judge"]
    if not criteria:
        return
    prompt = transcript(run, scenario, for_judge=True) + "\n\nCriteria:\n" + "\n".join(
        f"{i}. {c}" for i, c in enumerate(criteria, 1))
    try:
        msg = provider.chat(s, key, [{"role": "system", "content": JUDGE_SYSTEM},
                                     {"role": "user", "content": prompt}], [])
        run.cost += float((msg.get("_usage") or {}).get("cost") or 0)
        text = msg.get("content") or ""
        verdicts = json.loads(text[text.index("["):text.rindex("]") + 1])
    except Exception as e:
        verdicts = [{"pass": False, "why": f"judge failed: {type(e).__name__}"}] * len(criteria)
    for c, v in zip(criteria, verdicts + [{"pass": False, "why": "no verdict"}] * len(criteria)):
        run.checks.append({"label": f"judge: {c}", "pass": bool(v.get("pass")), "why": str(v.get("why", ""))})


def transcript(run: Run, scenario: dict[str, Any], for_judge: bool = False) -> str:
    lines = [] if for_judge else [f"# {run.scenario} / {run.model}", "", scenario.get("description", ""), ""]
    role = "Guest" if scenario.get("role") == "guest" else "Owner"
    for i, t in enumerate(run.turns):
        lines.append(f"**{role}:** {t['user']}\n")
        for c in (x for x in run.calls if x["turn"] == i + 1):
            lines.append(f"- tool `{c['name']}` {json.dumps(c['args'], ensure_ascii=False)[:400]}")
            result = c.get("result")
            if result is None:
                result = "(no result recorded: the turn ended before it was sent back)"
            if for_judge or len(result) <= 300:
                lines.append("  result: " + result.replace("\n", "\n  "))
            else:
                lines.append("  result: " + result[:300].replace("\n", "\n  ") + f" ... [{len(result)} chars]")
        if t["error"]:
            lines.append(f"\n**Error:** {t['error']}\n")
        else:
            lines.append(f"\n**Chelus:** {t['reply']}\n")
    if run.asked:
        lines.append("Approval cards shown: " + "; ".join(run.asked))
    if not for_judge:
        lines.append("\n## Checks\n")
        lines += [f"- {'PASS' if c['pass'] else 'FAIL'} {c['label']}{(' - ' + c['why']) if c['why'] else ''}"
                  for c in run.checks]
        lines.append(f"\n{run.model_calls} model calls, {len(run.calls)} tool calls, {run.prompt_tokens} in / "
                     f"{run.completion_tokens} out tokens, ${run.cost:.4f}, {run.seconds:.0f}s")
        notable = [f"{k}: {v}" for k, v in run.events if k in ("guard", "denied", "notice", "retry", "wait")]
        if notable:
            lines.append("\n## Agent notices\n")
            lines += [f"- {n[:300]}" for n in notable]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sim", description=__doc__.splitlines()[0])
    ap.add_argument("--models", default="gpt-oss-20b", help="comma-separated profiles from bench/models")
    ap.add_argument("--scenarios", default="all", help="comma-separated names from bench/scenarios, or all")
    ap.add_argument("--owner", default="haiku-5.5", help="profile for the simulated owner")
    ap.add_argument("--judge", default="haiku-5.5", help="profile for the judge (none to skip)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default="/out")
    ap.add_argument("--state", default="/state/runs")
    a = ap.parse_args(argv)

    models = a.models.split(",")
    for m in models:
        load_profile(m)
    scenarios = load_scenarios(a.scenarios)
    owner = side_settings(a.owner) if any("owner" in sc for sc in scenarios) else None
    jud = side_settings(a.judge) if a.judge != "none" else None
    out = Path(a.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    results: list[Run] = []
    for sc in scenarios:
        for m in models:
            for r in range(a.repeat):
                tag = f"{sc['name']} / {m}" + (f" #{r + 1}" if a.repeat > 1 else "")
                print(f"running {tag} ...", flush=True)
                name = f"{sc['name']}--{m}" + (f"--{r + 1}" if a.repeat > 1 else "")
                run = run_scenario(sc, m, Path(a.state) / out.name / name, owner)
                if jud:
                    judge(run, sc, *jud)
                (out / f"{name}.md").write_text(transcript(run, sc), encoding="utf-8")
                (out / f"{name}.json").write_text(json.dumps(run.__dict__, indent=1, default=str), encoding="utf-8")
                print(f"  {run.passed}/{len(run.checks)} checks, {len(run.calls)} tool calls, "
                      f"${run.cost:.4f}, {run.seconds:.0f}s{'  ERROR ' + run.error if run.error else ''}", flush=True)
                results.append(run)
    write_summary(out, results, models)
    print(f"\n{(out / 'summary.md').read_text(encoding='utf-8')}\nfull results: {out}")
    return 0


def write_summary(out: Path, results: list[Run], models: list[str]) -> None:
    rows = ["| scenario | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for name in dict.fromkeys(r.scenario for r in results):
        cells = []
        for m in models:
            rs = [r for r in results if r.scenario == name and r.model == m]
            passed = sum(r.passed for r in rs)
            total = sum(len(r.checks) for r in rs)
            calls = sum(len(r.calls) for r in rs) / len(rs)
            cells.append(f"{passed}/{total} ({calls:.0f} calls)")
        rows.append(f"| {name} | " + " | ".join(cells) + " |")
    totals = []
    for m in models:
        rs = [r for r in results if r.model == m]
        totals.append(f"{sum(r.passed for r in rs)}/{sum(len(r.checks) for r in rs)}, "
                      f"${sum(r.cost for r in rs):.3f}, {sum(r.seconds for r in rs):.0f}s")
    rows.append("| **total** | " + " | ".join(totals) + " |")
    (out / "summary.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
