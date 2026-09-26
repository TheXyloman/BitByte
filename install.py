"""Install Tufty AI Pet for the current macOS or Windows user."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent
CODEX_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop", "Interrupt", "SessionEnd")
CLAUDE_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Notification",
                 "PostToolUseFailure", "StopFailure", "Stop", "SessionEnd", "Elicitation", "ElicitationResult")
MARKERS = ("tufty-ai-pet/hook.py", "tufty-ai-pet/approval_hook.py")


def command(python: Path, source: str, event: str) -> str:
    runtime = python.parent.parent.parent
    script = "hook.py"
    parts = [str(python), str(runtime / script), source]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def group(python: Path, source: str, event: str) -> dict:
    timeout = 3 if event in {"SessionEnd", "Interrupt"} else 5
    return {"hooks": [{"type": "command", "command": command(python, source, event), "timeout": timeout}]}


def update_hooks(data: dict, events: tuple[str, ...], python: Path, source: str) -> dict:
    hooks = data.setdefault("hooks", {})
    # Remove only our handlers, even from mixed groups or obsolete event registrations.
    for event, existing in hooks.items():
        retained = []
        for item in existing:
            if not isinstance(item, dict):
                retained.append(item)
                continue
            handlers = item.get("hooks", [])
            cleaned = [handler for handler in handlers if not (
                isinstance(handler, dict) and any(marker in handler.get("command", "").replace("\\", "/")
                                                 for marker in MARKERS))]
            if cleaned or not handlers:
                retained.append({**item, "hooks": cleaned})
        hooks[event] = retained
    for event in events:
        hooks.setdefault(event, []).append(group(python, source, event))
    return data


def backup_and_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        backup.write_bytes(path.read_bytes())
        print("Backed up", path, "to", backup)
    temp = path.with_name(path.name + ".tufty-tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def install_hooks(home: Path, python: Path) -> None:
    codex_path = home / ".codex" / "hooks.json"
    claude_path = home / ".claude" / "settings.json"
    for path, events, source in ((codex_path, CODEX_EVENTS, "codex"),
                                 (claude_path, CLAUDE_EVENTS, "claude")):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object in " + str(path))
        update_hooks(data, events, python, source)
        backup_and_write(path, data)
        print("Installed", source, "hooks:", ", ".join(events))


def install_venv(runtime: Path) -> Path:
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ("bridge.py", "hook.py", "observer.py", "pet.py", "requirements.txt"):
        shutil.copy2(ROOT / name, runtime / name)
    # A running assistant may retain the old hook command until it restarts.
    # Keep only a non-blocking forwarder; the old decision/wait implementation is gone.
    (runtime / "approval_hook.py").write_text(
        '"""Compatibility entrypoint for cached pre-upgrade hook definitions."""\n'
        'from hook import main\n\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")
    env = runtime / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(runtime / "requirements.txt")], check=True)
    return python


def install_autostart(home: Path, python: Path, port: str | None) -> None:
    runtime = python.parent.parent.parent
    args = [str(python), str(runtime / "bridge.py")]
    # Startup always discovers USB devices; never pin the setup-time port.
    if sys.platform == "darwin":
        label = "com.codex.tufty-ai-pet"
        path = home / "Library" / "LaunchAgents" / (label + ".plist")
        path.parent.mkdir(parents=True, exist_ok=True)
        log = runtime / "bridge.log"
        payload = {"Label": label, "ProgramArguments": args, "RunAtLoad": True,
                   "KeepAlive": True, "StandardOutPath": str(log), "StandardErrorPath": str(log)}
        with path.open("wb") as stream:
            plistlib.dump(payload, stream)
        domain = "gui/" + str(os.getuid())
        subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
        result = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
        if result.returncode:
            print("LaunchAgent installed; login again to start it. launchctl:", result.stderr.strip())
        else:
            print("Bridge starts at login and is running now.")
    elif os.name == "nt":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            args[0] = str(pythonw)
        result = subprocess.run(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", "TuftyAIPet",
                                 "/TR", subprocess.list2cmdline(args), "/F"], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Task Scheduler setup failed: " + result.stderr.strip())
        subprocess.run(["schtasks", "/Run", "/TN", "TuftyAIPet"], check=False)
        print("Bridge starts at login and has been launched now.")
    else:
        raise SystemExit("Automatic startup is implemented for macOS and Windows only")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Deprecated setup hint; startup always discovers USB devices")
    parser.add_argument("--no-autostart", action="store_true", help="Install hooks but launch the bridge manually")
    args = parser.parse_args()
    if sys.platform != "darwin" and os.name != "nt":
        raise SystemExit("This installer supports macOS and Windows")
    if sys.platform == "darwin":
        runtime = Path.home() / "Library" / "Application Support" / "tufty-ai-pet"
    else:
        runtime = Path(os.environ["LOCALAPPDATA"]) / "tufty-ai-pet"
    python = install_venv(runtime)
    install_hooks(Path.home(), python)
    if not args.no_autostart:
        install_autostart(Path.home(), python, args.port)
    print("Next: connect and flash the Tufty, then review/trust the new Codex hooks with /hooks.")


if __name__ == "__main__":
    main()
