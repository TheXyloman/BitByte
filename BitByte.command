#!/bin/sh
# Double-click this file after unzipping a BitByte release on macOS.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'BitByte needs Python 3.10 or newer. Install it from https://www.python.org/downloads/macos/ and run this again.'
  read -r _
  exit 1
fi
exec python3 "$ROOT/setup_wizard.py"
