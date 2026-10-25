"""What an approval prompt shows the owner, and what the owner can answer.

A Request is a str (the old one-line summary, used in logs and by callers that only need
text) that also carries what a prompt needs to be readable on a phone:

- title: what the action is, written by the code ("Run a shell command"). Always true.
- purpose: why, in the model's own words (the `purpose` argument). Useful, not trusted:
  shown labeled as the model's words, under the facts.
- detail: the facts the code can show: the command, the file and size, a skill diff.
- choices: the answers on offer. Shell commands and file writes get once/deny, since each
  one is different. Skill writes also get "this session" and "always for this skill"
  (Hermes's once/session/always/deny ladder, applied per skill).

An approve callback returns True/False or one of the choice strings; `decision` folds
either into a choice.
"""

from __future__ import annotations

ONCE, SESSION, ALWAYS, DENY = "once", "session", "always", "deny"
CHOICE_LABELS = {
    ONCE: "Allow once",
    SESSION: "Allow for this conversation",
    ALWAYS: "Always for this skill",
    DENY: "Deny",
}


class Request(str):
    title: str
    purpose: str
    detail: str
    choices: tuple[str, ...]
    skill: str | None

    def __new__(cls, summary: str, *, title: str = "", purpose: str = "", detail: str = "",
                choices: tuple[str, ...] = (ONCE, DENY), skill: str | None = None) -> "Request":
        self = super().__new__(cls, summary)
        self.title = title or summary
        self.purpose = (purpose or "").strip()
        self.detail = detail
        self.choices = choices
        self.skill = skill
        return self

    def card(self) -> str:
        """The prompt as plain text, facts first."""
        lines = [self.title]
        lines.append(f"Why (Chelus's words): {self.purpose}" if self.purpose else "Why: (no reason given)")
        if self.detail:
            lines += ["", self.detail]
        return "\n".join(lines)


def decision(answer: object) -> str:
    """Fold an approve callback's answer into a choice. Anything unknown is a denial."""
    if answer is True:
        return ONCE
    if isinstance(answer, str) and answer in (ONCE, SESSION, ALWAYS):
        return answer
    return DENY
