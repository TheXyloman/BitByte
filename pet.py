"""Content-free events and session state for BitByte."""
from __future__ import annotations

from dataclasses import dataclass, field
import getpass
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time

_user = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
EVENT_FILE = Path("/tmp" if os.name != "nt" else tempfile.gettempdir()) / ("tufty-ai-pet-" + _user + ".jsonl")
STATES = {"idle", "working", "attention", "approval", "error", "done"}
QUESTION_TOOLS = {"request_user_input", "request_user_input_async", "AskUserQuestion"}
PLAN_TOOLS = {"ExitPlanMode"}


def tool_name(value):
    return value.rsplit(".", 1)[-1] if isinstance(value, str) else ""


def final_reason(text: str) -> str | None:
    """Conservative, local heuristic. Never retain the supplied reply."""
    if not isinstance(text, str):
        return None
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    text = re.sub(r"`[^`]*`", "", text)
    if re.search(r"<proposed_plan>\s*\S.*?</proposed_plan>", text, re.S):
        return "plan"
    # Only a direct closing request counts; optional offers are not blockers.
    tail = text.strip()[-1500:]
    for line in tail.splitlines()[-5:]:
        line = line.strip(" *-\t")
        if re.search(r"\b(if (?:you|needed)|optional|happy to|I can also|would you like me to)\b", line, re.I):
            continue
        if re.search(r"\b(?:please (?:choose|select|confirm|provide|tell|approve)|I need (?:your|you to)|(?:can|could) you (?:confirm|provide|choose|select|clarify)|which .{0,100}(?:do you|should (?:I|we))|(?:should|shall) (?:I|we) (?:proceed|implement|continue)|what .{0,80}(?:do you prefer|should (?:I|we) use))\b", line, re.I):
            return "question"
    return None


def normalize(source: str, payload: dict) -> dict | None:
    if source not in {"codex", "claude"} or not isinstance(payload, dict):
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 128:
        return None
    name = payload.get("hook_event_name")
    tool = tool_name(payload.get("tool_name"))
    mapping = {"UserPromptSubmit": "working", "PreToolUse": "working", "PostToolUse": "working",
               "PermissionRequest": "attention", "PostToolUseFailure": "error", "StopFailure": "error",
               "Stop": "done", "Interrupt": "idle", "SessionEnd": "idle",
               "Elicitation": "attention", "ElicitationResult": "working"}
    state = mapping.get(name)
    reason = None
    action = "activity"
    if name == "UserPromptSubmit":
        action = "start"
    elif name in {"Interrupt", "SessionEnd"}:
        action = "end"
    elif name == "StopFailure":
        action = "complete"
    elif name == "PermissionRequest":
        reason, action = "permission", "wait"
    elif name == "Elicitation":
        reason, action = "question", "wait"
    elif name == "ElicitationResult":
        action = "resolve"
    elif name == "PreToolUse" and tool in QUESTION_TOOLS | PLAN_TOOLS:
        state, reason, action = "attention", "plan" if tool in PLAN_TOOLS else "question", "wait"
    elif name in {"PostToolUse", "PostToolUseFailure"}:
        action = "activity" if tool == "request_user_input_async" else "resolve"
    elif name == "Stop":
        reason = final_reason(payload.get("last_assistant_message"))
        action = "final"
        if reason:
            state = "attention"
    if source == "codex" and name == "PostToolUse":
        result = payload.get("tool_response")
        if isinstance(result, dict):
            code = result.get("exit_code", result.get("exitCode"))
            if (isinstance(code, int) and not isinstance(code, bool) and code != 0) or result.get("isError") is True:
                state = "error"
    if name == "Notification":
        notice = payload.get("notification_type")
        if notice in {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}:
            state, action = "attention", "wait"
            reason = "permission" if notice == "permission_prompt" else "question"
        elif notice in {"elicitation_complete", "elicitation_response"}:
            state, action = "working", "resolve"
        else:
            return None  # idle_prompt also follows ordinary successful completion.
    if state is None:
        return None
    event = {"source": source, "session": session, "state": state, "action": action, "at": time.time()}
    if reason:
        event["reason"] = reason
    if tool:
        event["tool"] = tool
    for src, dst in (("turn_id", "turn"), ("tool_use_id", "request"), ("call_id", "request"),
                     ("elicitation_id", "request")):
        value = payload.get(src)
        if isinstance(value, str) and 0 < len(value) <= 256:
            event[dst] = value
    if name in {"Elicitation", "ElicitationResult"} or (name == "Notification" and "elicitation" in str(payload.get("notification_type"))):
        event["tool"] = "elicitation"
    # Registration metadata for the observer, never transcript contents.
    path = payload.get("transcript_path")
    if source == "codex" and isinstance(path, str) and len(path) <= 4096:
        event["transcript"] = path
    return event


@dataclass
class Session:
    state: str = "idle"
    at: float = 0
    turn: str = ""
    pending: dict = field(default_factory=dict)
    resolved: dict = field(default_factory=dict)
    final: str | None = None
    ended: bool = False


class PetState:
    def __init__(self):
        self.sessions: dict[str, Session] = {}

    def apply(self, event: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if (not isinstance(event, dict) or not isinstance(event.get("source"), str) or event.get("source") not in {"codex", "claude"}
                or not isinstance(event.get("state"), str) or event.get("state") not in STATES or not isinstance(event.get("session"), str)
                or not 0 < len(event["session"]) <= 128):
            return
        key = event["source"] + ":" + event["session"]
        item = self.sessions.setdefault(key, Session())
        at = event.get("at", now)
        if not isinstance(at, (float, int)) or not math.isfinite(at):
            return
        action = event.get("action", "wait" if event["state"] in {"attention", "approval"} else "activity")
        turn = event.get("turn", "")
        if not isinstance(action, str) or not isinstance(turn, str):
            return
        if at < item.at and (action in {"start", "end", "final", "complete"} or item.ended or (turn and item.turn and turn != item.turn)):
            return
        if action == "end" or event["state"] == "idle":
            item.pending.clear()
            item.final = None
            item.state, item.at, item.ended = "idle", at, True
            return
        if action == "start":
            item.pending.clear()
            item.final = None
            item.ended = False
        elif item.ended:
            return
        if turn:
            item.turn = turn
        request = event.get("request")
        tool = event.get("tool", "")
        reason = event.get("reason", "permission" if event["state"] == "approval" else "question")
        if (not isinstance(tool, str) or len(tool) > 256 or not isinstance(request, (str, type(None)))
                or (request is not None and len(request) > 256) or not isinstance(reason, str) or reason not in {"permission", "question", "plan"}):
            return
        token = request or reason + ":" + tool
        if action == "wait":
            if not tool and not request and any(value.get("reason") == reason for value in item.pending.values()):
                return  # A delayed notification duplicates an already tracked request.
            if at > max(item.resolved.get(token, -1), item.resolved.get("tool:" + tool, -1) if tool else -1):
                if token not in item.pending:
                    logging.info("Attention requested: %s (%s)", reason, event["source"])
                item.pending[token] = {"tool": tool, "at": at, "identified": bool(request), "reason": reason}
        elif action == "resolve":
            if tool and not request:
                item.resolved["tool:" + tool] = at
            if request:
                item.resolved[request] = at
            for pending, value in list(item.pending.items()):
                if (pending == request or (tool and value["tool"] == tool and (not request or not value.get("identified", False)))) and value["at"] <= at:
                    del item.pending[pending]
                    item.resolved[pending] = at
        elif action == "complete":
            # Async questions remain actionable after the assistant finishes its turn.
            item.pending = {key: value for key, value in item.pending.items()
                            if value.get("tool") == "request_user_input_async"}
            if event["state"] == "attention":
                item.final = reason
            elif event["state"] == "error":
                item.final = None
        elif action == "final":
            item.final = reason if event["state"] == "attention" else None
            # Uncorrelated notification waits are superseded by a final reply.
            for pending, value in list(item.pending.items()):
                if value.get("reason") == "permission" or pending == "question:":
                    item.pending.pop(pending, None)
        if len(item.resolved) > 512:
            item.resolved = dict(sorted(item.resolved.items(), key=lambda pair: pair[1])[-256:])
        if at >= item.at:
            item.state = "working" if event["state"] in {"attention", "approval"} else event["state"]
            item.at = at

    def current(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        values = list(self.sessions.values())
        if any(item.pending or item.final for item in values):
            return "attention"
        if any(item.state == "error" and now - item.at <= 5 for item in values):
            return "error"
        if any(item.state == "working" for item in values):
            return "working"
        if any(item.state == "done" and now - item.at <= 5 for item in values):
            return "done"
        return "idle"

    def snapshot(self):
        from dataclasses import asdict
        return {key: asdict(value) for key, value in self.sessions.items()}

    def restore(self, data):
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            try:
                if key.split(":", 1)[0] not in {"codex", "claude"} or value["state"] not in STATES:
                    continue
                item = Session(**value)
                if (not isinstance(item.at, (int, float)) or not math.isfinite(item.at)
                        or not isinstance(item.pending, dict) or not isinstance(item.resolved, dict)
                        or not isinstance(item.turn, str) or item.final not in {None, "plan", "permission", "question"}):
                    continue
                if any(not isinstance(v, dict) or not isinstance(v.get("tool"), str) or not isinstance(v.get("at"), (int, float)) for v in item.pending.values()):
                    continue
                if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in item.resolved.values()):
                    continue
                self.sessions[key] = item
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
