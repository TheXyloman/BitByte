"""Local event and session observer with automatic Tufty USB discovery."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time

from observer import CodexObserver, JsonlTail
from pet import EVENT_FILE, PetState

STATE_FILE = Path(__file__).resolve().parent / "state.json"


def append(path: Path, event: dict) -> None:
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def candidate_ports(preferred=None, serial_number=None):
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    def priority(item):
        label = " ".join(str(getattr(item, key, "") or "") for key in ("description", "manufacturer", "product")).lower()
        known = getattr(item, "vid", None) == 0x2E8A or "tufty" in label or "micropython" in label
        return (0 if serial_number and getattr(item, "serial_number", None) == serial_number else 1,
                0 if known else 1, item.device)
    candidates = [item.device for item in sorted(ports, key=priority)
                  if getattr(item, "vid", None) is not None or getattr(item, "hwid", "").startswith("USB")
                  or "usb" in item.device.lower() or priority(item)[1] == 0]
    return list(dict.fromkeys(([preferred] if preferred else []) + candidates))


def connect_tufty(preferred=None, serial_number=None):
    import serial
    for port in candidate_ports(preferred, serial_number):
        device = None
        verified = False
        try:
            device = serial.Serial(port, 115200, timeout=0.05, write_timeout=0.2)
            device.reset_input_buffer()
            device.write(b"HELLO\n")
            deadline = time.monotonic() + 1.5
            answer = b""
            while time.monotonic() < deadline:
                answer = (answer + device.read(64))[-256:]
                if b"TUFTY_PET/1" in answer.splitlines():
                    verified = True
                    logging.info("Connected to %s", port)
                    return device
            logging.debug("No pet handshake on %s", port)
        except (serial.SerialException, OSError):
            logging.debug("USB candidate unavailable: %s", port)
        finally:
            if device is not None and not verified:
                try:
                    device.close()
                except OSError:
                    pass
    return None


class USBLink:
    """One background connector; the main loop remains responsive during probing."""
    def __init__(self, preferred=None, serial_number=None):
        self.preferred, self.serial_number = preferred, serial_number
        self.device = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tufty-usb")
        self.future = None
        self.next_connect = 0
        self.next_presence = 0
        self.sent = None
        self.last_send = 0

    def disconnect(self, now):
        if self.device is not None:
            try:
                self.device.close()
            except OSError:
                pass
        self.device, self.sent = None, None
        self.next_connect = now + 3

    def tick(self, mood, now=None):
        from serial.tools import list_ports
        now = time.monotonic() if now is None else now
        if self.future is not None and self.future.done():
            try:
                self.device = self.future.result()
            except Exception as exc:
                logging.warning("USB discovery failed: %s", type(exc).__name__)
            self.future = None
            self.next_connect = now + 3
            self.sent = None
            if self.device is not None:
                for item in list_ports.comports():
                    if item.device == self.device.port:
                        self.serial_number = getattr(item, "serial_number", None) or self.serial_number
        if self.device is None and self.future is None and now >= self.next_connect:
            self.future = self.executor.submit(connect_tufty, self.preferred, self.serial_number)
        if self.device is None:
            return
        try:
            if now >= self.next_presence:
                self.next_presence = now + 1
                if self.device.port not in {item.device for item in list_ports.comports()}:
                    raise OSError("USB device removed")
            # Drain old firmware's button messages, without interpreting them.
            self.device.read(128)
            if mood != self.sent or now - self.last_send >= 10:
                self.device.write(("S " + mood + "\n").encode("ascii"))
                self.sent, self.last_send = mood, now
        except Exception as exc:
            logging.info("USB connection lost: %s", type(exc).__name__)
            self.disconnect(now)

    def close(self):
        self.disconnect(time.monotonic())
        self.executor.shutdown(wait=True)
        if self.future is not None:
            try:
                device = self.future.result()
                if device:
                    device.close()
            except Exception:
                pass


def load_checkpoint(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("version") == 1 else {}
    except (OSError, ValueError):
        return {}


def save_checkpoint(path, state, events, observer, serial_number):
    data = {"version": 1, "sessions": state.snapshot(), "events": events.snapshot(),
            "observer": observer.snapshot(), "serial_number": serial_number}
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, separators=(",", ":"))
    temp.replace(path)


def send_demo():
    for mood, action in (("working", "start"), ("attention", "final"), ("working", "start"),
                         ("error", "activity"), ("done", "final"), ("idle", "end")):
        append(EVENT_FILE, {"source": "codex", "session": "demo", "state": mood, "action": action, "at": time.time()})
        print(mood)
        time.sleep(3 if mood != "idle" else 0.1)


def run(port=None):
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("Install requirements.txt in a virtual environment first: pyserial is missing") from exc
    saved = load_checkpoint(STATE_FILE)
    state = PetState()
    state.restore(saved.get("sessions", {}))
    observer = CodexObserver(saved=saved.get("observer", {}))
    EVENT_FILE.touch(mode=0o600, exist_ok=True)
    events = JsonlTail(EVENT_FILE, saved.get("events"), start_at_end=not saved, max_line=16384)
    link = USBLink(port, saved.get("serial_number"))
    last_save, previous = 0, None
    try:
        while True:
            batch = events.read()
            for event in batch:
                if event.get("source") == "codex" and event.get("transcript"):
                    observer.register(event["transcript"], event.get("session"))
            batch.extend(observer.poll())
            for event in sorted(batch, key=lambda event: event.get("at", 0) if isinstance(event.get("at", 0), (float, int)) else 0):
                state.apply(event)
            mood = state.current()
            if mood != previous:
                logging.info("Mood %s -> %s", previous, mood)
                previous = mood
            link.tick(mood)
            if time.monotonic() - last_save >= 2:
                try:
                    save_checkpoint(STATE_FILE, state, events, observer, link.serial_number)
                except OSError as exc:
                    logging.warning("State checkpoint unavailable: %s", type(exc).__name__)
                last_save = time.monotonic()
            time.sleep(0.2)
    finally:
        try:
            save_checkpoint(STATE_FILE, state, events, observer, link.serial_number)
        finally:
            link.close()


def main():
    handler = RotatingFileHandler(Path(__file__).resolve().parent / "bridge.log", maxBytes=512 * 1024, backupCount=2)
    logging.basicConfig(handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Try this port first, then automatically discover other USB ports")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    try:
        send_demo() if args.demo else run(args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
