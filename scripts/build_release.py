#!/usr/bin/env python3
"""Create the unsigned, self-contained BitByte setup ZIP and its checksum file."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
INCLUDED = (
    "BitByte.command", "BitByte-Setup.cmd", "setup_wizard.py", "setup_services.py", "install.py",
    "bridge.py", "hook.py", "observer.py", "pet.py", "approval_hook.py", "requirements.txt", "README.md",
)
DIRECTORIES = ("firmware", "vendor")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(128 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def files(root: Path) -> list[Path]:
    found = [root / name for name in INCLUDED]
    for folder in DIRECTORIES:
        found.extend(path for path in (root / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc"))
    missing = [str(path.relative_to(root)) for path in found if not path.is_file()]
    if missing:
        raise SystemExit("Release is missing: " + ", ".join(missing))
    return sorted(found)


def write_manifest(root: Path) -> Path:
    manifest = {"format": 1, "files": {str(path.relative_to(root)): digest(path) for path in files(root)}}
    target = root / "release-manifest.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def build(version: str) -> Path:
    manifest = write_manifest(ROOT)
    DIST.mkdir(exist_ok=True)
    output = DIST / f"BitByte-{version}.zip"
    with tempfile.TemporaryDirectory() as temp:
        stage = Path(temp) / "BitByte"
        stage.mkdir()
        for path in files(ROOT) + [manifest]:
            destination = stage / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage.parent))
    (DIST / (output.name + ".sha256")).write_text(digest(output) + "  " + output.name + "\n", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="dev")
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args()
    if args.manifest_only:
        print(write_manifest(ROOT))
    else:
        print(build(args.version))


if __name__ == "__main__":
    main()
