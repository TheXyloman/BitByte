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
LABEL = "com.codex.tufty-ai-pet"


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


def hook_paths(home: Path) -> tuple[tuple[Path, tuple[str, ...], str], ...]:
    """The wizard deliberately configures both assistants, whether installed yet or not."""
    return ((home / ".codex" / "hooks.json", CODEX_EVENTS, "codex"),
            (home / ".claude" / "settings.json", CLAUDE_EVENTS, "claude"))


def _read_hook_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise ValueError("Could not read valid JSON from " + str(path)) from exc
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object in " + str(path))
    return data


def _write_hook_updates(updates: list[tuple[Path, dict]]) -> None:
    """Write all validated hook files or restore every original file on an I/O failure."""
    originals: dict[Path, bytes | None] = {path: path.read_bytes() if path.exists() else None for path, _ in updates}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    created: list[Path] = []
    try:
        for path, _ in updates:
            path.parent.mkdir(parents=True, exist_ok=True)
            if originals[path] is not None:
                backup = path.with_name(path.name + ".bak-" + stamp)
                backup.write_bytes(originals[path])
                print("Backed up", path, "to", backup)
        for path, data in updates:
            temp = path.with_name(path.name + ".tufty-tmp")
            temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temp.replace(path)
            created.append(path)
    except OSError:
        for path in created:
            try:
                if originals[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(originals[path])
            except OSError:
                pass
        raise


def install_hooks(home: Path, python: Path) -> None:
    updates = []
    for path, events, source in hook_paths(home):
        data = _read_hook_file(path)
        update_hooks(data, events, python, source)
        updates.append((path, data))
    _write_hook_updates(updates)
    for path, events, source in hook_paths(home):
        print("Installed", source, "hooks:", ", ".join(events))


def install_venv(runtime: Path, bundle: Path = ROOT) -> Path:
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ("bridge.py", "hook.py", "observer.py", "pet.py", "requirements.txt"):
        shutil.copy2(bundle / name, runtime / name)
    # A running assistant may retain the old hook command until it restarts.
    # Keep only a non-blocking forwarder; the old decision/wait implementation is gone.
    (runtime / "approval_hook.py").write_text(
        '"""Compatibility entrypoint for cached pre-upgrade hook definitions."""\n'
        'from hook import main\n\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")
    env = runtime / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    wheelhouse = bundle / "vendor"
    pip = [str(python), "-m", "pip", "install", "-r", str(runtime / "requirements.txt")]
    if wheelhouse.is_dir():
        pip.extend(["--no-index", "--find-links", str(wheelhouse)])
    subprocess.run(pip, check=True)
    return python


def install_autostart(home: Path, python: Path, port: str | None) -> None:
    runtime = python.parent.parent.parent
    args = [str(python), str(runtime / "bridge.py")]
    # Startup always discovers USB devices; never pin the setup-time port.
    if sys.platform == "darwin":
        label = LABEL
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


def stop_autostart(home: Path) -> None:
    """Stop our bridge before a firmware transfer claims its serial port."""
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + LABEL], capture_output=True)
    elif os.name == "nt":
        subprocess.run(["schtasks", "/End", "/TN", "TuftyAIPet"], capture_output=True)


def uninstall_hooks(home: Path) -> None:
    updates = []
    for path, _, _ in hook_paths(home):
        if not path.exists():
            continue
        data = _read_hook_file(path)
        update_hooks(data, (), Path("/not-used"), "unused")
        updates.append((path, data))
    if updates:
        _write_hook_updates(updates)


def uninstall(home: Path | None = None) -> None:
    home = home or Path.home()
    stop_autostart(home)
    if sys.platform == "darwin":
        (home / "Library" / "LaunchAgents" / (LABEL + ".plist")).unlink(missing_ok=True)
    elif os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/TN", "TuftyAIPet", "/F"], capture_output=True)
    uninstall_hooks(home)


def install_current(*, no_autostart: bool = False, bundle: Path = ROOT) -> Path:
    """Reusable host install used by the wizard and the legacy command line."""
    if sys.platform != "darwin" and os.name != "nt":
        raise SystemExit("Automatic setup is implemented for macOS and Windows only")
    if sys.platform == "darwin":
        runtime = Path.home() / "Library" / "Application Support" / "tufty-ai-pet"
    else:
        runtime = Path(os.environ["LOCALAPPDATA"]) / "tufty-ai-pet"
    python = install_venv(runtime, bundle)
    install_hooks(Path.home(), python)
    if not no_autostart:
        install_autostart(Path.home(), python, None)
    return python


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Deprecated setup hint; startup always discovers USB devices")
    parser.add_argument("--no-autostart", action="store_true", help="Install hooks but launch the bridge manually")
    args = parser.parse_args()
    install_current(no_autostart=args.no_autostart)
    print("Next: connect and flash the Tufty, then review/trust the new Codex hooks with /hooks.")


if __name__ == "__main__":
    main()
