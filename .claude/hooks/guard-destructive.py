#!/usr/bin/env python3
"""PreToolUse guard for Bash: warn + require confirmation on destructive commands.

Reads the Claude Code PreToolUse hook payload on stdin. If the command matches a
destructive pattern, returns permissionDecision "ask" (surfaces the reason and
forces an explicit confirm) rather than "deny", so intentional destructive work
is still possible — just never silent. Exit 0 with no decision = let normal
permission flow proceed.

Patterns are matched against the normalized command string; flag order is
collapsed so `rm -rf` and `rm -f -r` both trip. This is a safety net, not a
sandbox — treat a prompt as "look before you leap", not "this is blocked".
"""
import json
import re
import sys

# (regex, human reason). Regexes run against a whitespace-normalized command.
RULES = [
    (r"\brm\b[^|;&\n]*\s-\w*r", "recursive file delete (rm -r/-rf)"),
    (r"\brm\b.*\bchild_monitor\.db\b", "deleting the local database file"),
    (r"\bgit\s+push\b.*(--force\b|--force-with-lease\b|\s-\w*f)", "force push (rewrites remote history)"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard (discards uncommitted work)"),
    (r"\bgit\s+clean\b.*-\w*[fd]", "git clean (deletes untracked files)"),
    (r"\bgit\s+checkout\s+--\s", "git checkout -- (discards file changes)"),
    (r"\b(alembic|.*alembic)\s+downgrade\b", "alembic downgrade (mutates the live DB schema)"),
    (r"\bdropdb\b", "dropping a database"),
    (r"\b(drop\s+table|drop\s+database|truncate\s+table|truncate\b)", "destructive SQL (DROP/TRUNCATE)"),
    (r"\bdelete\s+from\b", "SQL DELETE (row loss if unscoped)"),
    (r">\s*\.env\b", "overwriting the .env secrets file"),
    (r"\bchmod\s+-R\b", "recursive permission change"),
    (r"\brm\s+-\w*r\w*\s+/(\s|$)", "delete targeting filesystem root"),
]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # malformed payload — don't get in the way

    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return 0

    normalized = re.sub(r"\s+", " ", command).lower()
    for pattern, reason in RULES:
        if re.search(pattern, normalized):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": f"Destructive command guard: {reason}. Confirm this is intended.",
                }
            }))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
