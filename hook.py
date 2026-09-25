"""Fast, non-blocking hook adapter. Never forwards task content."""

from __future__ import annotations

import json
import os
import sys

from pet import EVENT_FILE, normalize


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = {}
    try:
        payload = json.load(sys.stdin)
        event = normalize(source, payload)
        if event:
            line = (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii")
            fd = os.open(EVENT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except (OSError, ValueError, TypeError):
        pass  # The pet must never interrupt an assistant turn.
    if isinstance(payload, dict) and payload.get("hook_event_name") == "Stop" and source == "codex":
        print("{}")  # Codex Stop hooks require JSON on stdout.


if __name__ == "__main__":
    main()
