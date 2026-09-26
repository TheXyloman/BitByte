"""Incremental local session observation. No conversation content is persisted."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from pet import QUESTION_TOOLS, PLAN_TOOLS, final_reason, tool_name


class JsonlTail:
    """Bounded reads, partial-line buffering, and replacement/truncation recovery."""
    def __init__(self, path, cursor=None, start_at_end=False, max_line=2 * 1024 * 1024):
        self.path = Path(path)
        self.offset = 0
        self.identity = None
        self.prefix = ""
        self.buffer = b""
        self.dropping = False
        self.max_line = max_line
        self.last_stat = None
        if isinstance(cursor, dict):
            self.offset = cursor.get("offset", 0)
            if not isinstance(self.offset, int) or self.offset < 0:
                self.offset = 0
            self.identity = cursor.get("identity")
            self.prefix = cursor.get("prefix", "")
        elif start_at_end:
            try:
                with self.path.open("rb") as stream:
                    stat = self.path.stat()
                    self.identity = [stat.st_dev, stat.st_ino]
                    self.prefix = hashlib.sha256(stream.read(min(64, stat.st_size))).hexdigest() if stat.st_size >= 64 else ""
                    self.offset = stat.st_size
            except OSError:
                pass

    def read(self, budget=256 * 1024):
        records = []
        try:
            stat = self.path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if signature == self.last_stat and self.offset == stat.st_size:
                return records
            self.last_stat = signature
            with self.path.open("rb") as stream:
                identity = [stat.st_dev, stat.st_ino]
                prefix = hashlib.sha256(stream.read(64)).hexdigest() if stat.st_size >= 64 else ""
                if (self.identity is not None and identity != self.identity) or stat.st_size < self.offset or (self.prefix and prefix != self.prefix):
                    self.offset, self.buffer, self.dropping = 0, b"", False
                self.identity, self.prefix = identity, prefix
                stream.seek(self.offset)
                chunk = stream.read(budget)
                self.offset += len(chunk)
        except OSError:
            return records
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if self.dropping:
                self.dropping = False
                continue
            if len(line) > self.max_line:
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
            except (ValueError, UnicodeError):
                continue
        if len(self.buffer) > self.max_line:
            self.buffer = b""
            self.dropping = True
        return records

    def snapshot(self):
        # Re-read an incomplete line after restart; never persist its content.
        return {"offset": self.offset - len(self.buffer), "identity": self.identity, "prefix": self.prefix}


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return time.time()


def message_text(payload):
    content = payload.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))


class CodexSession:
    def __init__(self, session="", turn="", calls=None):
        self.session, self.turn = session, turn
        self.calls = calls if isinstance(calls, dict) else {}

    def convert(self, record):
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            return []
        kind, typ = record.get("type"), payload.get("type")
        if kind == "session_meta":
            self.session = payload.get("id", payload.get("session_id", self.session))
            return []
        if kind == "turn_context":
            self.turn = payload.get("turn_id", self.turn)
            return []
        if not self.session:
            return []
        base = {"source": "codex", "session": self.session, "at": timestamp(record.get("timestamp"))}
        if self.turn:
            base["turn"] = self.turn
        def emit(state, action="activity", **extra):
            return [{**base, "state": state, "action": action, **extra}]
        if kind == "event_msg":
            if typ in {"task_started", "user_message"}:
                if payload.get("turn_id"):
                    self.turn = payload["turn_id"]
                    base["turn"] = self.turn
                self.calls.clear()
                return emit("working", "start")
            if typ in {"turn_aborted", "session_end"}:
                self.calls.clear()
                return emit("idle", "end")
            if typ in {"task_complete", "task_completed"}:
                text = payload.get("last_agent_message", payload.get("last_assistant_message"))
                if isinstance(text, str):
                    reason = final_reason(text)
                    return emit("attention" if reason else "done", "complete", **({"reason": reason} if reason else {}))
                return emit("done", "complete")
            return []
        if kind != "response_item":
            return []
        if typ == "message":
            if payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                reason = final_reason(message_text(payload))
                return emit("attention" if reason else "done", "final", **({"reason": reason} if reason else {}))
            return []
        if typ in {"function_call", "custom_tool_call"}:
            tool = tool_name(payload.get("name"))
            request = payload.get("call_id")
            if not isinstance(request, str) or not tool:
                return []
            self.calls[request] = tool
            if tool in QUESTION_TOOLS | PLAN_TOOLS:
                return emit("attention", "wait", request=request, tool=tool, reason="plan" if tool in PLAN_TOOLS else "question")
            return emit("working")
        if typ in {"function_call_output", "custom_tool_call_output"}:
            request = payload.get("call_id")
            if not isinstance(request, str):
                return []
            tool = self.calls.pop(request, "")
            if not tool:
                return []
            if tool == "request_user_input_async":
                # Its immediate result only acknowledges that the question was shown.
                # A subsequent user message or turn start resolves it.
                return []
            return emit("working", "resolve", request=request, tool=tool)
        return []

    def snapshot(self):
        return {"session": self.session, "turn": self.turn, "calls": self.calls}


class CodexObserver:
    def __init__(self, root=None, saved=None):
        self.root = Path(root) if root else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
        self.files = {}
        self.next_scan = 0
        for path, value in (saved if isinstance(saved, dict) else {}).items():
            try:
                self.files[path] = (JsonlTail(path, value["cursor"]), CodexSession(**value["session"]))
            except (TypeError, KeyError):
                continue
        self.discover(initial=True)

    def register(self, path, session):
        if not isinstance(path, str) or not isinstance(session, str):
            return
        path = str(Path(path).expanduser())
        if path in self.files:
            self.files[path][1].session = session
        elif Path(path).is_file():
            # A live hook registered this file: reading from its current end avoids old turns.
            self.files[path] = (JsonlTail(path, start_at_end=True), CodexSession(session))

    def discover(self, initial=False):
        # Discover only today's files. Persisted/registered older active sessions remain followed.
        now = datetime.now()
        directory = self.root / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        try:
            paths = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:64]
        except OSError:
            return
        for path in paths:
            key = str(path)
            if key in self.files:
                continue
            session = ""
            try:
                with path.open("rb") as stream:
                    meta = json.loads(stream.readline(65536)).get("payload", {})
                    session = meta.get("id", meta.get("session_id", ""))
            except (OSError, ValueError, AttributeError):
                pass
            self.files[key] = (JsonlTail(path, start_at_end=initial), CodexSession(session))

    def poll(self):
        if time.monotonic() >= self.next_scan:
            self.discover()
            self.next_scan = time.monotonic() + 3
        events = []
        # Each stream read is bounded; rotate traversal to avoid starvation.
        keys = list(self.files)[:64]
        for path in keys:
            reader, session = self.files.pop(path)
            for record in reader.read(64 * 1024):
                events.extend(session.convert(record))
            self.files[path] = (reader, session)
        return events

    def snapshot(self):
        return {path: {"cursor": reader.snapshot(), "session": session.snapshot()} for path, (reader, session) in self.files.items()}
