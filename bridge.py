"""Local event-file-to-USB bridge for Tufty AI Pet."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import time

from pet import ACTION_FILE, EVENT_FILE, PetState


def append(path: Path, event: dict) -> None:
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def remember_approval(event: dict, pending: dict[str, float]) -> None:
    """Store only an opaque request id and expiry, never the requested action."""
    request, expires = event.get("request"), event.get("expires")
    if (event.get("state") == "approval" and isinstance(request, str) and len(request) == 32
            and isinstance(expires, int) and expires > time.time()):
        pending[request] = float(expires)


def handle_button(command: bytes, pending: dict[str, float]) -> None:
    decisions = {b"BUTTON A": "allow", b"BUTTON C": "deny"}
    decision = decisions.get(command.strip())
    if decision is None:
        return
    now = time.time()
    for request, expires in list(pending.items()):
        if expires <= now:
            del pending[request]
    if not pending:
        return
    request = next(reversed(pending))
    del pending[request]
    append(ACTION_FILE, {"request": request, "decision": decision})
    logging.info("Tufty button sent %s for a pending approval", decision)


def read_buttons(device, pending: dict[str, float], buffer: bytearray) -> None:
    """Read physical button messages from the pet without blocking its animations."""
    while True:
        chunk = device.read(64)
        if not chunk:
            break
        buffer.extend(chunk)
    while b"\n" in buffer:
        line, _, rest = buffer.partition(b"\n")
        buffer[:] = rest
        handle_button(line, pending)
    if len(buffer) > 128:
        buffer.clear()


def candidate_ports(override: str | None = None) -> list[str]:
    if override:
        return [override]
    from serial.tools import list_ports

    ports = []
    for item in list_ports.comports():
        label = " ".join(str(x or "") for x in (item.description, item.manufacturer, item.product)).lower()
        if item.vid == 0x2E8A or "tufty" in label or "micropython" in label:
            ports.append(item.device)
    return ports


def connect_tufty(override: str | None = None):
    import serial

    for port in candidate_ports(override):
        try:
            device = serial.Serial(port, 115200, timeout=0.05, write_timeout=0.2)
            device.reset_input_buffer()
            device.write(b"HELLO\n")
            deadline = time.monotonic() + 1.5
            answer = b""
            while time.monotonic() < deadline:
                answer += device.read(64)
                if b"TUFTY_PET/1" in answer:
                    logging.info("Connected to %s", port)
                    return device
            device.close()
        except (serial.SerialException, OSError):
            continue
    return None


def send_demo() -> None:
    for state in ("working", "attention", "approval", "working", "error", "done", "idle"):
        event = {"source": "codex", "session": "demo", "state": state}
        append(EVENT_FILE, event)
        print(state)
        time.sleep(3 if state != "idle" else 0.1)


def run(port: str | None = None) -> None:
    try:
        import serial  # noqa: F401 — fail early with a useful error if dependency is missing.
    except ImportError as exc:
        raise SystemExit("Install requirements.txt in a virtual environment first: pyserial is missing") from exc

    state = PetState()
    device = None
    sent = None
    last_send = 0.0
    next_connect = 0.0
    pending: dict[str, float] = {}
    button_buffer = bytearray()
    EVENT_FILE.touch(mode=0o600, exist_ok=True)
    ACTION_FILE.touch(mode=0o600, exist_ok=True)
    with EVENT_FILE.open("r", encoding="ascii") as events:
        events.seek(0, os.SEEK_END)  # Ignore stale events from before startup.
        logging.info("Watching %s", EVENT_FILE)
        while True:
            try:
                while True:
                    line = events.readline()
                    if not line:
                        break
                    if len(line) <= 512:
                        event = json.loads(line)
                        state.apply(event)
                        remember_approval(event, pending)
            except (UnicodeError, ValueError, TypeError):
                pass  # Ignore malformed events and continue with the next line.

            now = time.monotonic()
            if device is None and now >= next_connect:
                device = connect_tufty(port)
                next_connect = now + 3
                sent = None
                button_buffer.clear()
            if device is not None:
                try:
                    read_buttons(device, pending, button_buffer)
                except Exception as exc:
                    logging.warning("Tufty disconnected: %s", type(exc).__name__)
                    try:
                        device.close()
                    except OSError:
                        pass
                    device = None
                    next_connect = now + 1
            desired = state.current(now)
            if device is not None and (desired != sent or now - last_send >= 10):
                try:
                    device.write(("S " + desired + "\n").encode("ascii"))
                    sent = desired
                    last_send = now
                except Exception as exc:
                    # pyserial raises its own exception family; reconnect on any write failure.
                    logging.warning("Tufty disconnected: %s", type(exc).__name__)
                    try:
                        device.close()
                    except OSError:
                        pass
                    device = None
                    next_connect = now + 1
            time.sleep(0.2)


def main() -> None:
    logging.basicConfig(filename=Path(__file__).resolve().parent / "bridge.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Serial port override, such as /dev/cu.usbmodem101 or COM3")
    parser.add_argument("--demo", action="store_true", help="Send a short synthetic state sequence to a running bridge")
    args = parser.parse_args()
    if args.demo:
        send_demo()
    else:
        try:
            run(args.port)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
