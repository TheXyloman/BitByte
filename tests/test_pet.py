import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pet import PetState, normalize, final_reason
from observer import JsonlTail, CodexSession, CodexObserver
from install import update_hooks, CLAUDE_EVENTS, CODEX_EVENTS, install_autostart
import hook
import bridge


def event(state, at=1, session="a", **extra):
    return {"source": "codex", "session": session, "state": state, "at": at, **extra}


class EventTests(unittest.TestCase):
    def test_private_content_discarded(self):
        value = normalize("codex", {"hook_event_name": "PreToolUse", "session_id": "a",
                                  "tool_name": "request_user_input", "tool_input": {"secret": "private"}})
        self.assertEqual(value["state"], "attention")
        self.assertNotIn("private", json.dumps(value))
        self.assertNotIn("tool_input", value)

    def test_question_and_plan_hooks(self):
        for source, tool in (("codex", "request_user_input"), ("codex", "functions.request_user_input"),
                             ("claude", "AskUserQuestion"), ("claude", "ExitPlanMode")):
            pet = PetState()
            payload = dict(session_id="a", tool_name=tool, tool_use_id="q", hook_event_name="PreToolUse")
            pet.apply(normalize(source, payload))
            self.assertEqual(pet.current(), "attention")
            payload["hook_event_name"] = "PostToolUse"
            pet.apply(normalize(source, payload))
            self.assertEqual(pet.current(), "working")

    def test_async_acknowledgement_does_not_resolve_question(self):
        pet = PetState()
        for name in ("PreToolUse", "PostToolUse"):
            pet.apply(normalize("codex", dict(session_id="a", tool_name="request_user_input_async", hook_event_name=name)))
        self.assertEqual(pet.current(), "attention")

    def test_elicitation_result_resolves(self):
        pet = PetState()
        for name, expected in (("Elicitation", "attention"), ("ElicitationResult", "working")):
            pet.apply(normalize("claude", dict(session_id="a", hook_event_name=name, elicitation_id="e")))
            self.assertEqual(pet.current(), expected)

    def test_permission_hook_never_returns_decision_or_waits(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"events"
            with patch.object(sys, "argv", ["hook.py", "codex"]), patch.object(sys, "stdin", io.StringIO(json.dumps(
                    dict(session_id="a", hook_event_name="PermissionRequest")))), patch.object(hook, "EVENT_FILE", path):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    hook.main()
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(json.loads(path.read_text())["state"], "attention")

    def test_stop_json_and_final_classification(self):
        for reply, state in (("Done.", "done"), ("<proposed_plan>Build it.</proposed_plan>", "attention"),
                             ("Please confirm the target environment.", "attention")):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/"events"
                with patch.object(sys, "argv", ["hook.py", "codex"]), patch.object(sys, "stdin", io.StringIO(json.dumps(
                        dict(session_id="a", hook_event_name="Stop", last_assistant_message=reply)))), patch.object(hook, "EVENT_FILE", path):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        hook.main()
                    self.assertEqual(output.getvalue(), "{}\n")
                    self.assertEqual(json.loads(path.read_text())["state"], state)
                    self.assertNotIn(reply, path.read_text())

    def test_optional_and_quoted_questions_are_not_attention(self):
        for text in ("Done. Would you like me to add tests?", "> Please confirm the choice.",
                     "```\n<proposed_plan>example</proposed_plan>\n```", "Why does this matter? It saves time.",
                     "If you want, please choose another colour.", "The tests pass.", "Use `<proposed_plan>example</proposed_plan>` as a marker."):
            self.assertIsNone(final_reason(text), text)

    def test_idle_notice_is_not_attention(self):
        self.assertIsNone(normalize("claude", dict(session_id="a", hook_event_name="Notification", notification_type="idle_prompt")))

    def test_failures(self):
        value = normalize("codex", dict(session_id="a", hook_event_name="PostToolUse", tool_response={"exitCode": 2}))
        self.assertEqual(value["state"], "error")


class StateTests(unittest.TestCase):
    def test_no_timeouts_for_work_or_wait(self):
        pet = PetState()
        pet.apply(event("working"))
        self.assertEqual(pet.current(1000), "working")
        pet.apply(event("attention", 2, request="q", action="wait"))
        self.assertEqual(pet.current(10000), "attention")

    def test_unrelated_tools_and_sessions_do_not_clear_wait(self):
        pet = PetState()
        pet.apply(event("attention", request="q", tool="AskUserQuestion", action="wait"))
        pet.apply(event("working", 2, tool="Bash", action="resolve"))
        pet.apply(event("done", 3, session="other", action="final"))
        self.assertEqual(pet.current(4), "attention")
        pet.apply(event("working", 5, request="q", tool="AskUserQuestion", action="resolve"))
        self.assertEqual(pet.current(6), "working")

    def test_overlapping_requests_resolve_independently(self):
        pet = PetState()
        for request in ("q1", "q2"):
            pet.apply(event("attention", request=request, tool="AskUserQuestion", action="wait"))
        pet.apply(event("working", 2, request="q1", tool="AskUserQuestion", action="resolve"))
        self.assertEqual(pet.current(2), "attention")
        pet.apply(event("working", 3, request="q2", tool="AskUserQuestion", action="resolve"))
        self.assertEqual(pet.current(3), "working")

    def test_duplicate_and_out_of_order_events(self):
        pet = PetState()
        pet.apply(event("working", 3, request="q", tool="AskUserQuestion", action="resolve"))
        pet.apply(event("attention", 2, request="q", tool="AskUserQuestion", action="wait"))
        self.assertEqual(pet.current(4), "working")
        pet.apply(event("idle", 5, action="end"))
        pet.apply(event("working", 4))
        self.assertEqual(pet.current(6), "idle")

    def test_delayed_other_request_is_not_suppressed(self):
        pet = PetState()
        pet.apply(event("working", 3, request="q1", tool="AskUserQuestion", action="resolve"))
        pet.apply(event("attention", 2, request="q2", tool="AskUserQuestion", action="wait"))
        self.assertEqual(pet.current(4), "attention")

    def test_notification_does_not_duplicate_structured_request(self):
        pet = PetState()
        pet.apply(event("attention", request="q", tool="Bash", reason="permission", action="wait"))
        pet.apply(event("attention", 2, reason="permission", action="wait"))
        pet.apply(event("working", 3, tool="Bash", request="q", action="resolve"))
        self.assertEqual(pet.current(4), "working")

    def test_unidentified_notification_survives_unrelated_tool(self):
        pet = PetState()
        pet.apply(event("attention", action="wait"))
        pet.apply(event("working", 2, tool="Bash", action="resolve"))
        self.assertEqual(pet.current(3), "attention")

    def test_plan_persists_until_new_prompt(self):
        pet = PetState()
        pet.apply(event("attention", action="final", reason="plan"))
        pet.apply(event("working", 2))
        self.assertEqual(pet.current(1000), "attention")
        pet.apply(event("working", 1001, action="start"))
        self.assertEqual(pet.current(1002), "working")

    def test_completion_and_error_remain_brief(self):
        for mood in ("done", "error"):
            pet = PetState()
            pet.apply(event(mood))
            self.assertEqual(pet.current(2), mood)
            self.assertEqual(pet.current(7), "idle")

    def test_turn_completion_clears_cancelled_tools_but_keeps_final_plan(self):
        pet = PetState()
        pet.apply(event("attention", action="wait", request="q", reason="question"))
        pet.apply(event("attention", 2, action="final", reason="plan"))
        pet.apply(event("done", 3, action="complete"))
        self.assertEqual(pet.current(4), "attention")
        self.assertEqual(pet.sessions['codex:a'].pending, {})

    def test_async_question_survives_turn_completion_until_user_reply(self):
        pet = PetState()
        pet.apply(event("attention", action="wait", request="q", tool="request_user_input_async"))
        pet.apply(event("done", 2, action="complete"))
        self.assertEqual(pet.current(3), "attention")
        pet.apply(event("working", 4, action="start"))
        self.assertEqual(pet.current(5), "working")

    def test_final_response_clears_denied_permission_without_tool_result(self):
        pet = PetState()
        pet.apply(event("attention", action="wait", tool="Bash", reason="permission"))
        pet.apply(event("done", 2, action="final"))
        self.assertEqual(pet.current(3), "done")

    def test_legacy_approval_and_priority(self):
        pet = PetState()
        for mood in ("done", "working", "error", "approval"):
            pet.apply(event(mood, session=mood))
        self.assertEqual(pet.current(2), "attention")

    def test_snapshot_contains_no_content_and_restores_pending(self):
        pet = PetState()
        pet.apply(event("attention", action="wait", request="q", reason="question", secret="PRIVATE"))
        saved = pet.snapshot()
        self.assertNotIn("PRIVATE", json.dumps(saved))
        restored = PetState()
        restored.restore(saved)
        self.assertEqual(restored.current(999), "attention")

    def test_invalid_events_ignored(self):
        pet = PetState()
        for value in ([], None, {}, event("unknown"), event("working", source="other")):
            pet.apply(value)
        self.assertEqual(pet.current(), "idle")


class ObserverTests(unittest.TestCase):
    def test_question_reply_and_plan(self):
        parser = CodexSession("a")
        pet = PetState()
        def feed(payload):
            for value in parser.convert({"type": "response_item", "payload": payload}):
                pet.apply(value)
        feed(dict(type="function_call", name="request_user_input", call_id="q", arguments='{"secret":"private"}'))
        self.assertEqual(pet.current(), "attention")
        self.assertNotIn("private", json.dumps(parser.snapshot()))
        feed(dict(type="function_call_output", call_id="q", output="private answer"))
        self.assertEqual(pet.current(), "working")
        feed(dict(type="message", role="assistant", phase="final_answer", content=[dict(text="<proposed_plan>Build it</proposed_plan>")]))
        self.assertEqual(pet.current(), "attention")

    def test_unknown_events_and_nonfinal_messages_ignored(self):
        parser = CodexSession("a")
        self.assertEqual(parser.convert({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": "Please confirm."}}), [])
        self.assertEqual(parser.convert({"type": "unknown", "payload": []}), [])
        self.assertEqual(parser.convert({"type": "response_item", "payload": {"type": "function_call_output", "call_id": []}}), [])

    def test_task_end_clears_attention(self):
        parser = CodexSession("a")
        pet = PetState()
        pet.apply(event("attention"))
        for value in parser.convert(dict(type="event_msg", payload=dict(type="turn_aborted"))):
            pet.apply(value)
        self.assertEqual(pet.current(), "idle")

    def test_fragmentation_malformed_truncation_and_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"events"
            path.write_bytes(b'{"a":')
            reader = JsonlTail(path)
            self.assertEqual(reader.read(), [])
            with path.open("ab") as stream:
                stream.write(b'1}\nbroken\n{"b":2}\n')
            self.assertEqual(reader.read(), [{"a": 1}, {"b": 2}])
            path.write_text('{"c":3}\n')
            self.assertEqual(reader.read(), [{"c": 3}])
            path.rename(Path(temp)/"old")
            path.write_text('{"d":4}\n')
            self.assertEqual(reader.read(), [{"d": 4}])

    def test_partial_cursor_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"events"
            path.write_text('{"a":1}\n{"b":')
            reader = JsonlTail(path)
            self.assertEqual(reader.read(), [{"a": 1}])
            restored = JsonlTail(path, reader.snapshot())
            with path.open("a") as stream:
                stream.write('2}\n')
            self.assertEqual(restored.read(), [{"b": 2}])

    def test_reads_are_bounded_and_skip_oversized_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"events"
            path.write_text('x'*100+'\n{"ok":true}\n')
            reader = JsonlTail(path, max_line=32)
            found = []
            for _ in range(10):
                found.extend(reader.read(20))
            self.assertEqual(found, [{"ok": True}])

    def test_existing_transcripts_not_replayed_and_new_records_detected(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)/datetime.now().strftime('%Y/%m/%d')
            folder.mkdir(parents=True)
            path = folder/'test.jsonl'
            path.write_text(json.dumps(dict(type="session_meta", payload=dict(id="a")))+'\n'+json.dumps(
                dict(type="response_item", payload=dict(type="function_call", name="request_user_input", call_id="old")))+'\n')
            observer = CodexObserver(root=temp)
            self.assertEqual(observer.poll(), [])
            with path.open('a') as stream:
                stream.write(json.dumps(dict(type="response_item", payload=dict(type="function_call", name="request_user_input", call_id="new")))+'\n')
            self.assertEqual(observer.poll()[0]['request'], 'new')


class InstallerTests(unittest.TestCase):
    def test_mixed_hooks_preserved_and_install_idempotent(self):
        data = {"theme": "dark", "hooks": {"PermissionRequest": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "existing-tool"},
            {"type": "command", "command": "python /tmp/tufty-ai-pet/approval_hook.py codex"}]}]}}
        for _ in range(2):
            update_hooks(data, CODEX_EVENTS, Path("/tmp/tufty-ai-pet/.venv/bin/python"), "codex")
        self.assertEqual(data['theme'], 'dark')
        self.assertEqual(len(data['hooks']['PermissionRequest']), 2)
        self.assertEqual(data['hooks']['PermissionRequest'][0]['hooks'][0]['command'], 'existing-tool')
        self.assertNotIn('approval_hook.py', json.dumps(data))
        self.assertIn('PreToolUse', data['hooks'])
        self.assertLess(data['hooks']['PermissionRequest'][1]['hooks'][0]['timeout'], 10)
        self.assertIn('ElicitationResult', CLAUDE_EVENTS)

    def test_upgrade_keeps_nonblocking_cached_hook_entrypoint(self):
        import install
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            python = runtime / '.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.touch()
            (runtime / 'approval_hook.py').write_text('OLD BLOCKING ADAPTER')
            with patch('install.subprocess.run'):
                install.install_venv(runtime)
            shim = runtime / 'approval_hook.py'
            self.assertNotIn('OLD BLOCKING', shim.read_text())
            # Execute the installed compatibility entrypoint with a local event sink.
            script = "import sys,runpy; sys.path.insert(0,sys.argv[1]); import pet; pet.EVENT_FILE=__import__('pathlib').Path(sys.argv[2]); sys.argv=[sys.argv[1]+'/approval_hook.py','codex']; runpy.run_path(sys.argv[0],run_name='__main__')"
            result = subprocess.run([sys.executable, '-c', script, str(runtime), str(runtime/'events')],
                                    input=json.dumps(dict(session_id='a', hook_event_name='PermissionRequest')),
                                    text=True, capture_output=True, timeout=3, check=True)
            self.assertEqual(result.stdout, '')
            self.assertEqual(json.loads((runtime/'events').read_text())['state'], 'attention')

    def test_startup_removes_fixed_port(self):
        import plistlib
        with tempfile.TemporaryDirectory() as temp, patch('install.sys.platform', 'darwin'), patch('install.subprocess.run') as run:
            run.return_value.returncode = 0
            install_autostart(Path(temp), Path(temp)/'tufty-ai-pet/.venv/bin/python', '/dev/old')
            path = Path(temp)/'Library/LaunchAgents/com.codex.tufty-ai-pet.plist'
            self.assertNotIn('--port', plistlib.loads(path.read_bytes())['ProgramArguments'])

    def test_windows_startup_does_not_pin_com_port(self):
        with tempfile.TemporaryDirectory() as temp, patch('install.sys.platform', 'win32'), patch('install.os.name', 'nt'), patch('install.subprocess.run') as run:
            run.return_value.returncode = 0
            install_autostart(__import__('pathlib').PosixPath(temp), __import__('pathlib').PosixPath(temp)/'python.exe', 'COM1')
            calls = [call.args[0] for call in run.call_args_list]
            create = next(args for args in calls if '/Create' in args)
            self.assertNotIn('--port', create[create.index('/TR')+1])



class SerialTests(unittest.TestCase):
    def modules(self, ports, device):
        list_ports = types.SimpleNamespace(comports=lambda: ports)
        return {'serial': types.SimpleNamespace(Serial=device, SerialException=OSError),
                'serial.tools': types.SimpleNamespace(list_ports=list_ports), 'serial.tools.list_ports': list_ports}

    def port(self, path, vid=0x2E8A, serial='pet'):
        return types.SimpleNamespace(device=path, vid=vid, serial_number=serial, hwid='USB', description='', manufacturer='', product='')

    def test_stale_preference_falls_back_and_serial_survives_renaming(self):
        ports = [self.port('COM9', 123, 'other'), self.port('COM8', serial='pet'), self.port('COM3', serial='another')]
        with patch.dict(sys.modules, self.modules(ports, None)):
            self.assertEqual(bridge.candidate_ports('COM1', 'pet'), ['COM1', 'COM8', 'COM3', 'COM9'])

    def test_busy_and_nonmatching_ports_closed_then_correct_handshake(self):
        devices = []
        class Device:
            def __init__(self, port, *args, **kwargs):
                if port == 'busy':
                    raise OSError()
                self.port, self.closed = port, False
                devices.append(self)
            def reset_input_buffer(self): pass
            def write(self, data): pass
            def read(self, count): return b'TUFTY_PET/1\n' if self.port == 'pet' else b'OTHER\n'
            def close(self): self.closed = True
        with patch.dict(sys.modules, self.modules([], Device)), patch.object(bridge, 'candidate_ports', return_value=['busy', 'other', 'pet']), patch.object(bridge.time, 'monotonic', side_effect=[0, 1, 2, 3, 4]):
            result = bridge.connect_tufty()
        self.assertEqual(result.port, 'pet')
        self.assertTrue(devices[0].closed)
        self.assertFalse(result.closed)

    def test_probe_read_error_closes_port(self):
        class Device:
            closed = False
            def __init__(self, *args, **kwargs): pass
            def reset_input_buffer(self): pass
            def write(self, data): raise OSError()
            def close(self): Device.closed = True
        with patch.dict(sys.modules, self.modules([], Device)), patch.object(bridge, 'candidate_ports', return_value=['bad']):
            self.assertIsNone(bridge.connect_tufty())
        self.assertTrue(Device.closed)

    def test_reconnect_sends_current_mood_and_ignores_buttons(self):
        from concurrent.futures import Future
        class Device:
            port = 'COM8'
            def __init__(self): self.writes=[]; self.closed=False
            def read(self, count): return b'BUTTON A\nBUTTON C\n'
            def write(self, value): self.writes.append(value)
            def close(self): self.closed=True
        device = Device()
        ports = [self.port('COM8')]
        with patch.dict(sys.modules, self.modules(ports, None)):
            link = bridge.USBLink()
            link.future = Future()
            link.future.set_result(device)
            link.tick('attention', 1)
            self.assertEqual(device.writes, [b'S attention\n'])
            ports.clear()
            link.tick('attention', 3)
            self.assertTrue(device.closed)
            self.assertIsNone(link.device)
            link.close()


if __name__ == '__main__':
    unittest.main()
