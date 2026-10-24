"""Checks on skill text before it is saved: a scanner that blocks dangerous content and a
linter that only advises.

Hermes's skills_guard has about 100 patterns aimed at third-party skills, and Hermes leaves
it off for skills its own agent writes. Chelys runs a subset on every skill write, because
its threat is laundering: text from a guest, a web page or a pasted message turning into a
skill that every later conversation obeys. So the subset is what launders: prompt
injection, invisible characters, secrets, sending data out, and instructions to change
Chelys's own config. Destructive commands only warn, since a skill may legitimately
document cleanup. A pattern preceded closely by a negation ("never send ...") is demoted
one level, as in Hermes.

Verdict: any critical finding blocks the write, with the findings named so the model can
fix the text. High findings are shown to the owner on the approval prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CRITICAL, HIGH = "critical", "high"


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    label: str
    line: int
    excerpt: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.label} (line {self.line}: {self.excerpt!r})"


_I = re.I
PATTERNS: list[tuple[str, str, str, re.Pattern]] = [
    # injection: text that tries to steer whoever reads the skill
    (CRITICAL, "injection", "tells the reader to ignore its instructions",
     re.compile(r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your|system|owner'?s?)\b[^.\n]{0,20}\b(instructions?|rules|prompt|guidelines|directives)", _I)),
    (CRITICAL, "injection", "tries to change who the assistant is",
     re.compile(r"\b(you are now|from now on you are|pretend (to be|you are)|act as if you (are|were)|new persona)\b", _I)),
    (CRITICAL, "injection", "tells the reader to hide something from the owner",
     re.compile(r"\b(do not|don'?t|never|without)\s+(tell|telling|inform|informing|mention|mentioning|notify|notifying|let)\b[^.\n]{0,20}\b(the )?(user|owner|human)\b", _I)),
    (CRITICAL, "injection", "claims to be a system message or prompt",
     re.compile(r"(<\s*/?\s*system\s*>|\[\s*system\s*(message|prompt)?\s*\]|\bsystem prompt\s*(override|:)|\bdeveloper mode\b|\bDAN\b|\bjailbreak)", _I)),
    (CRITICAL, "injection", "asks to send the conversation, prompt or memory somewhere",
     re.compile(r"\b(send|post|upload|share|leak|forward|exfiltrate|reveal|print)\b[^.\n]{0,30}\b(conversation|chat history|context|system prompt|instructions|memory|history)\b[^.\n]{0,30}\b(to|at|via)\b", _I)),
    (HIGH, "injection", "hidden HTML comment or invisible element",
     re.compile(r"(<!--[\s\S]{0,400}?-->|display\s*:\s*none|visibility\s*:\s*hidden)", _I)),
    # secrets
    (CRITICAL, "secret", "contains what looks like an API key or token",
     re.compile(r"(\bsk-[A-Za-z0-9_-]{16,}|\bghp_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\b\d{6,12}:[A-Za-z0-9_-]{30,}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----)")),
    # exfiltration
    (CRITICAL, "exfiltration", "sends a secret-looking variable over the network",
     re.compile(r"\b(curl|wget|fetch|requests\.(get|post)|urllib|nc|ncat)\b[^\n]{0,200}\$\{?\w*(KEY|TOKEN|SECRET|PASS)\w*", _I)),
    (CRITICAL, "exfiltration", "reads Chelys's key, token or history files",
     re.compile(r"(deepseek\.key|telegram\.token|telegram_user_id|history\.sqlite3|\.config/chelys|DEEPSEEK_API_KEY|\.ssh/id_)", _I)),
    (CRITICAL, "exfiltration", "uses a known data-drop site",
     re.compile(r"\b(webhook\.site|requestbin|pipedream\.net|pastebin\.com|transfer\.sh|ngrok\.io|hookbin|interact\.sh|burpcollaborator)\b", _I)),
    # persistence and self-modification
    (CRITICAL, "persistence", "tells the reader to change Chelys's settings or start-up files",
     re.compile(r"\b(edit|modify|change|write|append|overwrite|update|add|replace|delete|remove)\b[^.\n]{0,40}(config\.toml|persona\.md|telegram_guests|AGENTS\.md|CLAUDE\.md|\.bashrc|\.profile|\.zshrc|authorized_keys|crontab|termux/boot|\.termux/boot)", _I)),
    # remote code and destruction: warn
    (HIGH, "remote-code", "pipes a download straight into a shell",
     re.compile(r"\b(curl|wget)\b[^\n|]{0,200}\|\s*(ba|z)?sh\b|base64\s+(-d|--decode)[^\n]{0,40}\|\s*(ba)?sh\b", _I)),
    (HIGH, "destructive", "deletes broadly or wipes a device",
     re.compile(r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|\$HOME|/sdcard|/storage)(\s|$|/\s|/\*)|\bmkfs\b|\bdd\b[^\n]{0,40}\bof=/dev/", _I)),
]

INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
NEGATION = re.compile(r"\b(never|don'?t|do not|must not|avoid|no)\b[^.\n]{0,25}$", _I)


def scan(text: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.split("\n")
    for n, line in enumerate(lines, 1):
        if INVISIBLE.search(line):
            findings.append(Finding(CRITICAL, "invisible-unicode",
                                    "contains invisible or text-direction Unicode characters", n,
                                    INVISIBLE.sub("<?>", line.strip())[:80]))
    for severity, category, label, pat in PATTERNS:
        for m in pat.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            line = lines[line_no - 1]
            col = m.start() - (text.rfind("\n", 0, m.start()) + 1)
            sev = severity
            # "never send the chat history to ..." is a rule, not an instruction; the hide-from-owner
            # pattern already includes its own negation, so it is not demoted.
            if label != "tells the reader to hide something from the owner" and NEGATION.search(line[:col]):
                sev = HIGH if severity == CRITICAL else None
            if sev is None:
                continue
            findings.append(Finding(sev, category, label, line_no, m.group(0).strip()[:80]))
    findings.sort(key=lambda f: (f.severity != CRITICAL, f.line))
    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == CRITICAL]


def block_message(found: list[Finding]) -> str:
    lines = "\n".join(f"- {f}" for f in found[:8])
    return ("refused: this skill text looks unsafe to save, because a saved skill is followed in every "
            f"later conversation:\n{lines}\n"
            "Remove or rewrite those parts. Skills must not carry instructions aimed at the reader, "
            "secrets, or text copied from untrusted sources as if the owner wrote it.")


# ---- lint: advice only, never blocks ----

INCIDENT_REF = re.compile(r"(\b(PR|issue|ticket)\s*#?\d+|#\d{2,}\b|\b20\d\d-\d\d-\d\d\b)", _I)
MARKETING = re.compile(r"\b(powerful|comprehensive|seamless|advanced|robust|cutting-edge|ultimate)\b", _I)


def lint(description: str, body: str) -> list[str]:
    notes = []
    if len(description) > 60:
        notes.append(f"description is {len(description)} chars; the skill index shows only the first 60")
    if MARKETING.search(description):
        notes.append("description uses marketing words; state the capability plainly")
    if "## When to Use" not in body and "## when to use" not in body.lower():
        notes.append("no '## When to Use' section; add trigger phrases so the skill gets loaded")
    refs = INCIDENT_REF.findall(body)
    if len(refs) >= 4:
        notes.append("reads like an incident log (dates, issue numbers); write lessons as rules plus "
                     "one clause of why")
    return notes


def new_lint(before: tuple[str, str] | None, after: tuple[str, str]) -> list[str]:
    """Findings the change introduced (on a patch, old problems aren't repeated)."""
    now = lint(*after)
    if before is None:
        return now
    old = set(lint(*before))
    return [n for n in now if n not in old]
