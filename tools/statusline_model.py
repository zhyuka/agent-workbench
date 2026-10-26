#!/usr/bin/env python3
"""Claude Code statusline: make the LIVE model impossible to miss.

WHY. Claude Code has been seen to drop silently from the pinned model to a smaller one at
the auto-compaction boundary and stay there until the next fresh user message. Neither a
`"model"` pin in settings.json nor an explicit `/model` command issued shortly before is
guaranteed to survive it. A hook cannot fix that where managed policy disables user hooks.

So the mitigation is visibility. A downgrade you can SEE costs one keystroke to undo
(any message restores the pinned model); a downgrade you cannot see costs a whole
work session at the wrong model.

Contract: Claude Code pipes a JSON blob on stdin and renders stdout as the status line.
Exit non-zero or print nothing and the line silently disappears -- so this never raises.
"""
import json
import os
import sys

# A model id not starting with this is treated as a downgrade and shouted about.
# $STATUSLINE_EXPECTED_MODEL overrides it.
EXPECTED_PREFIX = os.environ.get("STATUSLINE_EXPECTED_MODEL") or "claude-opus"

# Set it about 50k below your `autoCompactWindow`, so the warning shows before compaction, not
# as it starts. Claude Code's own `exceeds_200k_tokens` flag is fixed at 200k, so count tokens
# here instead. $STATUSLINE_COMPACT_WARN_TOKENS overrides it.
try:
    COMPACT_WARN_TOKENS = int(os.environ.get("STATUSLINE_COMPACT_WARN_TOKENS") or 150000)
except ValueError:
    COMPACT_WARN_TOKENS = 150000

RESET = "\033[0m"
RED = "\033[1;97;41m"     # white on red, bold
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("Usage: wired as `statusLine` in ~/.claude/settings.json; Claude Code pipes a JSON")
        print("blob on stdin and renders stdout as the status line. Not run by hand.")
        print("  echo '{\"model\":{\"id\":\"claude-sonnet-4-6\"}}' | python tools/statusline_model.py")
        return

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    model = data.get("model") or {}
    mid = str(model.get("id") or "")
    name = str(model.get("display_name") or mid or "unknown")

    ws = data.get("workspace") or {}
    cwd = str(ws.get("current_dir") or data.get("cwd") or "")
    short = os.path.basename(cwd.rstrip("/\\")) or cwd

    parts = []

    if mid.startswith(EXPECTED_PREFIX):
        parts.append("%s%s%s" % (CYAN, name, RESET))
    else:
        # The whole point of this script.
        parts.append("%s  !! MODEL=%s -- NOT %s, SEND ANY MESSAGE TO RESET  %s"
                     % (RED, mid or name, EXPECTED_PREFIX.upper(), RESET))

    if short:
        parts.append("%s%s%s" % (DIM, short, RESET))

    cw = data.get("context_window") or {}
    used = (cw.get("total_input_tokens") or 0) + (cw.get("total_output_tokens") or 0)
    if used > COMPACT_WARN_TOKENS:
        parts.append("%sctx>%dk (compaction near -- model may flip)%s"
                     % (YELLOW, COMPACT_WARN_TOKENS // 1000, RESET))

    sys.stdout.write("  ".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let the statusline break the UI.
        sys.stdout.write("statusline error")
    sys.exit(0)
