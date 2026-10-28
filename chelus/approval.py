"""What an approval prompt shows the owner, and what the owner can answer.

A Request is a str (the old one-line summary, used in logs and by callers that only need
text) that also carries what a prompt needs to be readable on a phone:

- title: what the action is, written by the code ("Run a shell command"). Always true.
- purpose: why, in the model's own words (the `purpose` argument). Useful, not trusted:
  shown labeled as the model's words, under the facts.
- detail: the facts the code can show: the command, the file and size, a skill diff.
- choices: ordinary risky tools and skill writes share the once/session/always/deny
  ladder. Always is exact-action scoped for ordinary tools and per-skill for skills.

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

    def label(self, choice: str) -> str:
        if choice == ALWAYS and not self.skill:
            return "Always this action"
        return CHOICE_LABELS[choice]

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


def manage(store, text: str = "") -> str:
    """Owner-only adapters call this; never send raw commands/content in grant listings."""
    parts = text.split()
    if not parts:
        lines = []
        for row in store.tool_grants():
            scope = (f"conversation #{row['session_id']} (ordinary risky tools)" if row['session_id']
                     else f"always this action: {row['tool']} [{row['action_key'][:12]}]")
            lines.append(f"#{row['id']}: {scope}")
        return "\n".join(lines) or "No ordinary tool approvals saved."
    if parts == ["clear"]:
        return f"Cleared {store.revoke_tool()} ordinary tool approval(s)."
    if len(parts) == 2 and parts[0] == "revoke" and parts[1].isascii() and parts[1].isdigit():
        grant_id = int(parts[1])
        if 0 < grant_id < 2**63:
            return (f"Revoked approval #{grant_id}." if store.revoke_tool(grant_id)
                    else "No such ordinary tool approval.")
    return "Usage: /approvals [revoke ID|clear] (CLI: chelus approvals [revoke ID|clear])"
