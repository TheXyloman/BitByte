"""Wait briefly for a physical Tufty approval button without reading task content."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import uuid

from pet import ACTION_FILE, EVENT_FILE

WAIT_SECONDS = 75


def append(path: Path, event: dict) -> None:
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def reply(decision: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision},
        }
    }, separators=(",", ":")))


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
        session = payload.get("session_id")
        if (source not in {"codex", "claude"} or payload.get("hook_event_name") != "PermissionRequest"
                or not isinstance(session, str) or not 0 < len(session) <= 128):
            return
        request = uuid.uuid4().hex
        ACTION_FILE.touch(mode=0o600, exist_ok=True)
        with ACTION_FILE.open("r", encoding="ascii") as actions:
            actions.seek(0, os.SEEK_END)
            append(EVENT_FILE, {
                "source": source,
                "session": session,
                "state": "approval",
                "request": request,
                "expires": int(time.time() + WAIT_SECONDS),
            })
            deadline = time.monotonic() + WAIT_SECONDS
            while time.monotonic() < deadline:
                line = actions.readline()
                if line:
                    try:
                        action = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if (action.get("request") == request
                            and action.get("decision") in {"allow", "deny"}):
                        reply(action["decision"])
                        return
                else:
                    time.sleep(0.05)
    except (OSError, TypeError, ValueError):
        # Let the coding assistant show its normal approval prompt on any failure.
        return


if __name__ == "__main__":
    main()
