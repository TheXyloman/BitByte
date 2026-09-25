"""Shared, content-free event protocol for the Tufty pet."""

from __future__ import annotations

from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import tempfile
import time

_user = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
EVENT_FILE = Path("/tmp" if os.name != "nt" else tempfile.gettempdir()) / ("tufty-ai-pet-" + _user + ".jsonl")
ACTION_FILE = Path("/tmp" if os.name != "nt" else tempfile.gettempdir()) / ("tufty-ai-pet-actions-" + _user + ".jsonl")
STATES = {"idle", "working", "attention", "approval", "error", "done"}


def normalize(source: str, payload: dict) -> dict | None:
    """Discard prompts, tool arguments and results before sending an event."""
    if source not in {"codex", "claude"} or not isinstance(payload, dict):
        return None
    name = payload.get("hook_event_name")
    mapping = {
        "UserPromptSubmit": "working",
        "PreToolUse": "working",
        "PostToolUse": "working",
        "PermissionRequest": "approval",
        "PostToolUseFailure": "error",
        "StopFailure": "error",
        "Stop": "done",
        "Interrupt": "idle",
        "SessionEnd": "idle",
    }
    state = mapping.get(name)
    if source == "codex" and name == "PostToolUse":
        result = payload.get("tool_response")
        if isinstance(result, dict):
            code = result.get("exit_code", result.get("exitCode"))
            if (isinstance(code, int) and not isinstance(code, bool) and code != 0) or result.get("isError") is True:
                state = "error"
    if name == "Notification":
        notification = payload.get("notification_type")
        if notification == "permission_prompt":
            state = "approval"
        elif notification in {"idle_prompt", "elicitation_dialog"}:
            state = "attention"
    if state is None:
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 128:
        return None
    return {"source": source, "session": session, "state": state}


@dataclass
class Session:
    state: str
    at: float


class PetState:
    """Pick one display state when more than one assistant session is active."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}

    def apply(self, event: dict, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if (not isinstance(event, dict) or event.get("source") not in {"codex", "claude"}
                or event.get("state") not in STATES
                or not isinstance(event.get("session"), str)
                or not 0 < len(event["session"]) <= 128):
            return
        key = event["source"] + ":" + event["session"]
        state = event["state"]
        if state == "idle":
            self.sessions.pop(key, None)
        else:
            self.sessions[key] = Session(state, now)

    def current(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        for key, item in list(self.sessions.items()):
            age = now - item.at
            lifetime = 5 if item.state in {"done", "error"} else (600 if item.state in {"approval", "attention"} else 120)
            if age > lifetime:
                del self.sessions[key]
        if not self.sessions:
            return "idle"
        values = list(self.sessions.values())
        # An unresolved approval needs attention even if another session is working.
        if any(item.state == "approval" for item in values):
            return "approval"
        if any(item.state == "attention" for item in values):
            return "attention"
        errors = [item for item in values if item.state == "error"]
        if errors:
            return "error"
        if any(item.state == "working" for item in values):
            return "working"
        return "done"
