import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install
import setup_services


class BundleTests(unittest.TestCase):
    def test_bundle_manifest_detects_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "payload.txt"
            payload.write_text("safe")
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            (root / "release-manifest.json").write_text(json.dumps({"format": 1, "files": {"payload.txt": digest}}))
            self.assertEqual(setup_services.verify_bundle(root)["format"], 1)
            payload.write_text("changed")
            with self.assertRaises(setup_services.SetupError):
                setup_services.verify_bundle(root)

    def test_unsafe_manifest_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "release-manifest.json").write_text(json.dumps({"format": 1, "files": {"../x": "0"}}))
            with self.assertRaises(setup_services.SetupError):
                setup_services.verify_bundle(root)


class FirmwareTests(unittest.TestCase):
    def test_backup_creates_zip_before_flash(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "backups"
            def copy_board(python, port, args, **kwargs):
                root = Path(args[-1])
                root.mkdir(parents=True)
                (root / "main.py").write_text("old firmware")
                return type("Result", (), {"stdout": ""})()
            with patch.object(setup_services, "_mpremote", side_effect=copy_board):
                archive = setup_services.backup_board(Path("python"), "COM5", destination)
            self.assertTrue(archive.is_file())
            self.assertEqual(archive.suffix, ".zip")

    def test_flash_requires_confirmation_after_backup(self):
        status = []
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(setup_services, "verify_uf2"), \
                patch.object(setup_services, "find_micropython_port", return_value="COM7"), \
                patch.object(setup_services, "backup_board", return_value=Path(temp) / "backup.zip"), \
                patch.object(setup_services, "wait_for_bootloader") as bootloader:
            with self.assertRaises(setup_services.SetupError):
                setup_services.guided_firmware_setup(Path("python"), Path(temp), status.append, lambda backup: False)
            bootloader.assert_not_called()

    def test_upload_verifies_every_file_size(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            firmware = root / "firmware"
            (firmware / "frames" / "idle").mkdir(parents=True)
            (firmware / "interactions" / "a").mkdir(parents=True)
            (firmware / "main.py").write_text("main")
            (firmware / "frames" / "idle" / "1.jpg").write_bytes(b"123")
            (firmware / "interactions" / "a" / "1.jpg").write_bytes(b"4567")
            calls = []
            def transfer(python, port, args, **kwargs):
                calls.append(args)
                if args[0] == "exec" and "os.stat" in args[1]:
                    name = args[1].split("'")[1]
                    size = (firmware / name).stat().st_size
                    return type("Result", (), {"stdout": str(size) + "\n"})()
                return type("Result", (), {"stdout": ""})()
            with patch.object(setup_services, "_mpremote", side_effect=transfer):
                setup_services.upload_firmware(Path("python"), "COM7", root, lambda _: None)
            self.assertEqual(calls[-1], ["reset"])
            self.assertEqual(sum(1 for call in calls if call[:2] == ["fs", "cp"]), 3)


class HookSafetyTests(unittest.TestCase):
    def test_invalid_second_hook_file_leaves_first_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex" / "hooks.json"
            codex.parent.mkdir()
            codex.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "keep"}]}]}}))
            claude = home / ".claude" / "settings.json"
            claude.parent.mkdir()
            claude.write_text("not json")
            original = codex.read_bytes()
            with self.assertRaises(ValueError):
                install.install_hooks(home, Path("/tmp/venv/bin/python"))
            self.assertEqual(codex.read_bytes(), original)

    def test_uninstall_removes_only_bitbyte_handlers(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            path = home / ".codex" / "hooks.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
                {"command": "existing"}, {"command": "python /tmp/tufty-ai-pet/hook.py codex"}]}]}}))
            install.uninstall_hooks(home)
            remaining = json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"]
            self.assertEqual(remaining, [{"command": "existing"}])


if __name__ == "__main__":
    unittest.main()
