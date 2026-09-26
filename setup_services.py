"""Shared, user-level services for the BitByte setup wizard and release builder."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable

ROOT = Path(__file__).resolve().parent
UF2_NAME = "tufty2040-v1.29.0-2-pimoroni-micropython.uf2"
UF2_PATH = ROOT / "firmware" / "uf2" / UF2_NAME
UF2_SHA256 = "5e7a04d989d899c0636d5c7c86a2c4e7dbce88d61bf3ca33651b873585fa5d9e"
MANAGED_FIRMWARE = ("main.py", "frames", "interactions")


class SetupError(RuntimeError):
    """An error suitable for display in a setup screen."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_supported_python() -> None:
    if sys.version_info < (3, 10):
        raise SetupError("BitByte requires Python 3.10 or newer. Install it from python.org, then run setup again.")


def verify_bundle(root: Path = ROOT) -> dict:
    """Verify all files declared by a release manifest before modifying a computer."""
    manifest_path = root / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError("This BitByte folder is incomplete: release-manifest.json is missing or invalid.") from exc
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise SetupError("This BitByte release manifest is not supported.")
    for name, expected in manifest["files"].items():
        path = root / name
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
            raise SetupError("The release manifest contains an unsafe file path.")
        if not path.is_file() or sha256(path) != expected:
            raise SetupError("Release verification failed for " + name + ". Download a fresh BitByte ZIP.")
    return manifest


def verify_uf2(path: Path = UF2_PATH) -> None:
    if not path.is_file() or sha256(path) != UF2_SHA256:
        raise SetupError("The bundled Tufty firmware is missing or did not pass its checksum check.")


def bootloader_volumes() -> list[Path]:
    """Return mounted RP2040 bootloader volumes without requiring elevated privileges."""
    if os.name != "nt":
        volume = Path("/Volumes/RPI-RP2")
        return [volume] if volume.is_dir() else []
    from ctypes import create_unicode_buffer, windll
    found: list[Path] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        label = create_unicode_buffer(261)
        if windll.kernel32.GetVolumeInformationW(drive, label, len(label), None, None, None, None, 0) and label.value == "RPI-RP2":
            found.append(Path(drive))
    return found


def wait_for_bootloader(timeout: float, status: Callable[[str], None]) -> Path:
    deadline = time.monotonic() + timeout
    status("Put the Tufty into bootloader mode: hold BOOTSEL, tap RESET, then release BOOTSEL.")
    while time.monotonic() < deadline:
        volumes = bootloader_volumes()
        if len(volumes) == 1:
            return volumes[0]
        if len(volumes) > 1:
            raise SetupError("More than one RPI-RP2 drive was found. Disconnect the other RP2040 board and try again.")
        time.sleep(1)
    raise SetupError("The RPI-RP2 bootloader drive did not appear. Check the USB data cable and retry the BOOTSEL/RESET step.")


def flash_uf2(volume: Path, uf2: Path = UF2_PATH) -> None:
    verify_uf2(uf2)
    if not volume.is_dir():
        raise SetupError("The Tufty bootloader drive disappeared before flashing could start.")
    try:
        shutil.copy2(uf2, volume / uf2.name)
    except OSError as exc:
        raise SetupError("Could not copy the Tufty firmware to RPI-RP2. Reconnect it and try again.") from exc


def _mpremote(python: Path, port: str, args: list[str], *, timeout: float = 45) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(python), "-m", "mpremote", "connect", port, *args], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError) as exc:
        raise SetupError("Could not communicate with the Tufty on " + port + ". Close Thonny or any serial terminal, then retry.") from exc


def _runtime_bridge(python: Path, script: str, *, timeout: float = 10) -> str:
    """Run serial discovery in BitByte's private venv, never in the user's Python install."""
    runtime = python.parent.parent.parent
    try:
        result = subprocess.run([str(python), "-c", script], cwd=runtime, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError) as exc:
        raise SetupError("BitByte's private USB runtime is unavailable. Run Repair, then try firmware setup again.") from exc
    return result.stdout.strip()


def find_micropython_port(python: Path) -> str | None:
    """Prefer the live BitByte handshake, otherwise probe one unambiguous USB serial board."""
    port = _runtime_bridge(python, "import bridge; d=bridge.connect_tufty(); print(d.port if d else '')")
    if port:
        return port
    candidates = json.loads(_runtime_bridge(python, "import bridge,json; print(json.dumps(bridge.candidate_ports()))") or "[]")
    if len(candidates) != 1:
        return None
    try:
        _mpremote(python, candidates[0], ["exec", "print('BITBYTE_PROBE')"], timeout=6)
    except SetupError:
        return None
    return candidates[0]


def backup_board(python: Path, port: str, destination: Path) -> Path:
    """Make a complete, local archive before a flash can erase the board filesystem."""
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = destination / f"tufty-backup-{stamp}"
    try:
        _mpremote(python, port, ["fs", "cp", "-r", ":", str(root)], timeout=120)
        archive = shutil.make_archive(str(root), "zip", root)
    except (OSError, SetupError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise SetupError("BitByte could not create a complete Tufty backup, so flashing was stopped.") from exc
    shutil.rmtree(root, ignore_errors=True)
    return Path(archive)


def firmware_files(root: Path = ROOT) -> list[Path]:
    files = [root / "firmware" / "main.py"]
    for directory in (root / "firmware" / "frames", root / "firmware" / "interactions"):
        files.extend(sorted(path for path in directory.rglob("*") if path.is_file() and "__pycache__" not in path.parts))
    return files


def _reset_managed_firmware(python: Path, port: str, files: list[Path], root: Path) -> None:
    """Remove only prior BitByte paths, then make the exact directory tree for this release."""
    directories = sorted({path.relative_to(root / "firmware").parent.as_posix() for path in files
                          if path.parent != root / "firmware"}, key=lambda value: (value.count("/"), value))
    clean = """import os
def remove_tree(path):
    try:
        for item in os.ilistdir(path):
            child = path + '/' + item[0]
            if item[1] & 0x4000:
                remove_tree(child)
            else:
                os.remove(child)
        os.rmdir(path)
    except OSError:
        pass
for path in ('frames', 'interactions'):
    remove_tree(path)
try:
    os.remove('main.py')
except OSError:
    pass
"""
    _mpremote(python, port, ["exec", clean], timeout=20)
    make = "import os\n"
    for directory in directories:
        make += "\ntry:\n os.mkdir(%r)\nexcept OSError:\n pass\n" % directory
    _mpremote(python, port, ["exec", make], timeout=20)


def upload_firmware(python: Path, port: str, root: Path = ROOT, status: Callable[[str], None] = print) -> None:
    files = firmware_files(root)
    if not files:
        raise SetupError("The firmware files are missing from this BitByte release.")
    _reset_managed_firmware(python, port, files, root)
    for index, source in enumerate(files, start=1):
        remote = source.relative_to(root / "firmware").as_posix()
        status(f"Uploading firmware {index}/{len(files)}: {remote}")
        _mpremote(python, port, ["fs", "cp", str(source), ":" + remote], timeout=45)
    # Check every expected filename and byte size without reading user files outside BitByte paths.
    for source in files:
        remote = source.relative_to(root / "firmware").as_posix()
        code = "import os; print(os.stat(%r)[6])" % remote
        result = _mpremote(python, port, ["exec", code], timeout=10)
        values = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        if not values or int(values[-1]) != source.stat().st_size:
            raise SetupError("Firmware verification failed for " + remote + ". The board was not started.")
    _mpremote(python, port, ["reset"], timeout=10)


def wait_for_tufty(python: Path, timeout: float, status: Callable[[str], None]) -> str:
    deadline = time.monotonic() + timeout
    status("Waiting for the Tufty to restart and verify BitByte firmware.")
    while time.monotonic() < deadline:
        port = find_micropython_port(python)
        if port:
            verified = _runtime_bridge(
                python,
                "import bridge; d=bridge.connect_tufty(%r); print('yes' if d else ''); d and d.close()" % port)
            if verified == "yes":
                return port
        time.sleep(1)
    raise SetupError("The Tufty did not answer the BitByte verification handshake. Retry Firmware Setup.")


@dataclass
class FirmwareResult:
    backup: Path | None
    port: str


def guided_firmware_setup(python: Path, backup_directory: Path, status: Callable[[str], None],
                          confirm_flash: Callable[[Path | None], bool], timeout: float = 90) -> FirmwareResult:
    """Run the destructive sequence only after a readable board has been backed up and confirmed."""
    assert_supported_python()
    verify_uf2()
    existing_port = find_micropython_port(python)
    backup = None
    if existing_port:
        status("Creating a required backup of the current Tufty filesystem.")
        backup = backup_board(python, existing_port, backup_directory)
        status("Backup saved to " + str(backup))
    else:
        status("No readable existing Tufty filesystem was found; this will be treated as a fresh board.")
    if not confirm_flash(backup):
        raise SetupError("Firmware flashing was cancelled. No changes were made to the board.")
    volume = wait_for_bootloader(timeout, status)
    status("Installing the bundled Pimoroni MicroPython firmware.")
    flash_uf2(volume)
    # The copy reboots the RP2040. Give the new USB serial interface time to enumerate.
    time.sleep(3)
    port = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and port is None:
        port = find_micropython_port(python)
        if port is None:
            time.sleep(1)
    if port is None:
        raise SetupError("MicroPython did not appear after flashing. Reconnect the Tufty and retry Firmware Setup.")
    upload_firmware(python, port, status=status)
    verified = wait_for_tufty(python, timeout, status)
    return FirmwareResult(backup, verified)
