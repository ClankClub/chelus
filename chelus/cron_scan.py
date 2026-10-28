"""The strict scan of a scheduled job's prompt, ported from Hermes's cronjob_prompt_scan.py.

A job prompt is short and written to be obeyed later, unattended, so it gets the strict
pattern set: a prompt that contains `cat ~/.ssh/id_rsa` or `rm -rf /` is a red flag, not
prose. It runs when a job is created or changed and again before every run, so a prompt
edited straight into the database is caught too. Pure standard library, so cron.py and the
CLI can use it without importing the agent.
"""

from __future__ import annotations

import re

from .skill_guard import INVISIBLE

THREATS = [
    (r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519|id_ecdsa)", "read_secrets"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"/etc/sudoers|visudo", "sudoers_mod"),
    (r"rm\s+-rf\s+/", "destructive_root_rm"),
]

_SECRET_VAR = r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?"
EXFIL = [
    (rf"curl\s+[^\n]*https?://[^\s\"'`]*{_SECRET_VAR}", "exfil_curl_url"),
    (rf"wget\s+[^\n]*https?://[^\s\"'`]*{_SECRET_VAR}", "exfil_wget_url"),
    (rf"curl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|-d|--form|-F)\s+[^\n]*{_SECRET_VAR}", "exfil_curl_data"),
    (rf"wget\s+[^\n]*--post-(?:data|file)=[^\n]*{_SECRET_VAR}", "exfil_wget_post"),
    (rf"curl\s+[^\n]*(?:-H|--header)\s+[\"']Authorization:\s*(?:Bearer|token)\s+{_SECRET_VAR}[\"']", "exfil_curl_auth_header"),
]

ZWJ = "‍"
# A zero-width joiner holds emoji sequences together, so it is fine between two emoji.
_EMOJI = ((0x1F000, 0x1FFFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x1F1E6, 0x1F1FF), (0x20E3, 0x20E3))
_VS16 = 0xFE0F


def _emoji(ch: str) -> bool:
    return any(lo <= ord(ch) <= hi for lo, hi in _EMOJI)


def _joins_emoji(text: str, i: int) -> bool:
    left, right = i - 1, i + 1
    while left >= 0 and ord(text[left]) == _VS16:
        left -= 1
    while right < len(text) and ord(text[right]) == _VS16:
        right += 1
    return left >= 0 and right < len(text) and _emoji(text[left]) and _emoji(text[right])


def scan(prompt: str) -> str:
    """"" when the prompt is fine, else why it is blocked (written for the model to read)."""
    for i, ch in enumerate(prompt):
        if INVISIBLE.match(ch) and not (ch == ZWJ and _joins_emoji(prompt, i)):
            return f"Blocked: the prompt contains an invisible character (U+{ord(ch):04X}), which can hide instructions."
    for patterns in (THREATS, EXFIL):
        for pattern, label in patterns:
            if re.search(pattern, prompt, re.IGNORECASE):
                return (f"Blocked: the prompt matches the threat pattern '{label}'. A scheduled job's prompt "
                        "must not contain injection or secret-stealing text.")
    return ""
