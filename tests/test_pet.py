import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet import PetState, normalize
from install import update_hooks, CLAUDE_EVENTS, CODEX_EVENTS
import hook
import bridge


class EventTests(unittest.TestCase):
    def test_sensitive_content_is_discarded(self):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "abc",
                   "prompt": "secret prompt", "tool_input": {"command": "secret command"}}
        self.assertEqual(normalize("codex", payload),
                         {"source": "codex", "session": "abc", "state": "working"})
        self.assertNotIn("secret", json.dumps(normalize("codex", payload)))

    def test_hook_emits_only_minimal_event_and_codex_stop_json(self):
        original_argv, original_stdin = sys.argv, sys.stdin
        try:
            sys.argv = ["hook.py", "codex"]
            sys.stdin = io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "abc",
                                                "last_assistant_message": "secret response"}))
            output = io.StringIO()
            with tempfile.TemporaryDirectory() as temp:
                event_file = Path(temp) / "events.jsonl"
                with patch.object(hook, "EVENT_FILE", event_file), contextlib.redirect_stdout(output):
                    hook.main()
                line = event_file.read_text()
            self.assertEqual(json.loads(line), {"source": "codex", "session": "abc", "state": "done"})
            self.assertNotIn("secret", line)
            self.assertEqual(output.getvalue(), "{}\n")
        finally:
            sys.argv, sys.stdin = original_argv, original_stdin

    def test_concurrent_sessions_and_expiry(self):
        pet = PetState()
        pet.apply({"source": "codex", "session": "a", "state": "working"}, 0)
        pet.apply({"source": "claude", "session": "b", "state": "approval"}, 1)
        self.assertEqual(pet.current(2), "approval")
        pet.apply({"source": "claude", "session": "b", "state": "done"}, 3)
        self.assertEqual(pet.current(4), "working")
        self.assertEqual(pet.current(121), "idle")

    def test_attention_is_distinct_from_approval_and_has_priority_over_work(self):
        pet = PetState()
        pet.apply({"source": "codex", "session": "work", "state": "working"}, 0)
        pet.apply({"source": "claude", "session": "notice", "state": "attention"}, 1)
        self.assertEqual(pet.current(2), "attention")
        pet.apply({"source": "claude", "session": "permission", "state": "approval"}, 3)
        self.assertEqual(pet.current(4), "approval")

    def test_claude_idle_and_elicitation_notifications_need_attention(self):
        for notification in ("idle_prompt", "elicitation_dialog"):
            payload = {"hook_event_name": "Notification", "notification_type": notification,
                       "session_id": "abc", "message": "private"}
            self.assertEqual(normalize("claude", payload),
                             {"source": "claude", "session": "abc", "state": "attention"})

    def test_unexpected_messages_do_not_change_pet(self):
        pet = PetState()
        pet.apply({"source": "elsewhere", "session": "x", "state": "approval"}, 0)
        pet.apply({"source": "codex", "session": "x", "state": "arbitrary"}, 0)
        self.assertEqual(pet.current(1), "idle")

    def test_codex_structured_tool_failure_is_error_without_output(self):
        payload = {"hook_event_name": "PostToolUse", "session_id": "abc",
                   "tool_response": {"exit_code": 1, "output": "private error details"}}
        self.assertEqual(normalize("codex", payload),
                         {"source": "codex", "session": "abc", "state": "error"})


class InstallerTests(unittest.TestCase):
    def test_existing_claude_hooks_are_preserved_and_install_is_idempotent(self):
        data = {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-tool"}]}]}}
        python = Path("/tmp/tufty-ai-pet/.venv/bin/python")
        update_hooks(data, CLAUDE_EVENTS, python, "claude")
        update_hooks(data, CLAUDE_EVENTS, python, "claude")
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "existing-tool")
        self.assertEqual(set(CLAUDE_EVENTS).difference(data["hooks"]), set())

    def test_permission_request_uses_the_physical_button_hook(self):
        data = update_hooks({}, CODEX_EVENTS, Path("/tmp/tufty-ai-pet/.venv/bin/python"), "codex")
        handler = data["hooks"]["PermissionRequest"][0]["hooks"][0]
        self.assertIn("approval_hook.py", handler["command"])
        self.assertEqual(handler["timeout"], 80)


class SerialTests(unittest.TestCase):
    def test_handshake_selects_only_pet_firmware(self):
        class FakeDevice:
            def __init__(self, port, *_, **__):
                self.port = port
                self.writes = []
                self.closed = False

            def reset_input_buffer(self):
                pass

            def write(self, data):
                self.writes.append(data)

            def read(self, _):
                return b"TUFTY_PET/1\n" if self.port == "pet" else b""

            def close(self):
                self.closed = True

        import types
        fake_serial = types.SimpleNamespace(Serial=FakeDevice, SerialException=OSError)
        with patch.dict(sys.modules, {"serial": fake_serial}), patch.object(bridge, "candidate_ports", return_value=["pet"]):
            result = bridge.connect_tufty()
        self.assertEqual(result.port, "pet")
        self.assertEqual(result.writes, [b"HELLO\n"])

    def test_a_and_c_buttons_return_one_physical_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            action_file = Path(temp) / "actions.jsonl"
            pending = {"a" * 32: 1000.0}
            with patch.object(bridge, "ACTION_FILE", action_file), patch.object(bridge.time, "time", return_value=10.0):
                bridge.handle_button(b"BUTTON A", pending)
            self.assertEqual(pending, {})
            self.assertEqual(json.loads(action_file.read_text()),
                             {"request": "a" * 32, "decision": "allow"})


if __name__ == "__main__":
    unittest.main()
