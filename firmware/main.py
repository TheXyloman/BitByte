"""Tufty 2040 pet firmware. Copy this file to the board as main.py."""

import select
import sys
import time
import urandom

import jpegdec
from picographics import PicoGraphics, DISPLAY_TUFTY_2040
from pimoroni import Button

display = PicoGraphics(display=DISPLAY_TUFTY_2040)
decoder = jpegdec.JPEG(display)
button_b = Button(8, invert=False)
button_up = Button(22, invert=False)
button_down = Button(6, invert=False)
poller = select.poll()
poller.register(sys.stdin, select.POLLIN)

FRAME_TIME_MS = 160
FOCUS_FRAME_TIME_MS = 2000
FRAME_COUNT = 6
NORMAL_BACKLIGHT = 0.75
# Tufty's backlight response is steep below half power; this remains visibly dim.
FOCUS_BACKLIGHT = 0.55
STATUS_TIME_MS = 3000
CONNECTION_TIME_MS = 15000
FRAMES = {
    "idle": "frames/idle/",
    "working": "frames/working/",
    "attention": "frames/approval/",
    "approval": "frames/approval/",
    "error": "frames/error/",
    "done": "frames/done/",
}

state = "idle"
last_host = time.ticks_ms()
interaction = None
interaction_started = 0
focus_mode = False
status_until = 0
was_pressed = {"B": False, "UP": False, "DOWN": False}
line = bytearray()
shown_mood = None
shown_frame = -1


def read_usb():
    global state, last_host, line, status_until
    while poller.poll(0):
        char = sys.stdin.buffer.read(1)
        if not char:
            return
        if char == b"\n":
            command = bytes(line).strip().decode("ascii", "ignore")
            line = bytearray()
            if command == "HELLO":
                sys.stdout.write("TUFTY_PET/1\n")
            elif command in ("S idle", "S working", "S attention", "S approval", "S error", "S done"):
                new_state = "attention" if command == "S approval" else command[2:]
                if new_state != state:
                    # A new live mood is more useful than an old manual status card.
                    status_until = 0
                state = new_state
                last_host = time.ticks_ms()
        elif len(line) < 32:
            line.extend(char)


def active_frame_time():
    if focus_mode and state in ("idle", "working") and interaction is None:
        return FOCUS_FRAME_TIME_MS
    return FRAME_TIME_MS


def set_backlight():
    if focus_mode and state in ("idle", "working") and interaction is None:
        display.set_backlight(FOCUS_BACKLIGHT)
    else:
        display.set_backlight(NORMAL_BACKLIGHT)


def draw_status(now):
    """Show a short local status card without exposing assistant content."""
    display.set_pen(display.create_pen(17, 31, 48))
    display.rectangle(0, 0, 320, 240)
    display.set_pen(display.create_pen(118, 230, 218))
    display.text("TUFTY STATUS", 18, 30, scale=3)
    connected = time.ticks_diff(now, last_host) <= CONNECTION_TIME_MS
    display.set_pen(display.create_pen(240, 245, 255))
    display.text("LINK: " + ("CONNECTED" if connected else "WAITING"), 18, 92, scale=2)
    display.text("MOOD: " + state.upper(), 18, 130, scale=2)
    display.text("FOCUS: " + ("ON" if focus_mode else "OFF"), 18, 168, scale=2)
    display.update()


def draw(now):
    """Show the next supplied animation frame only when it changes."""
    global interaction, shown_mood, shown_frame
    set_backlight()
    if time.ticks_diff(status_until, now) > 0:
        # Keep the confirmation/status card easy to read even when focus is on.
        display.set_backlight(NORMAL_BACKLIGHT)
        if shown_mood == "status":
            return
        draw_status(now)
        shown_mood = "status"
        shown_frame = 0
        return
    if shown_mood == "status":
        shown_mood = None
    frame_time = active_frame_time()
    if interaction is not None:
        elapsed = time.ticks_diff(now, interaction_started)
        if elapsed < FRAME_TIME_MS * FRAME_COUNT:
            mood = "interaction-" + interaction
            path = "interactions/" + interaction + "/"
            frame = elapsed // FRAME_TIME_MS + 1
        else:
            interaction = None
    if interaction is None:
        mood = state
        path = FRAMES[mood]
        frame = (now // frame_time) % FRAME_COUNT + 1
    if mood == shown_mood and frame == shown_frame:
        return
    try:
        decoder.open_file(path + str(frame) + ".jpg")
        decoder.decode(0, 0, jpegdec.JPEG_SCALE_FULL, dither=True)
        display.update()
        shown_mood = mood
        shown_frame = frame
    except OSError:
        # Keep the previous image visible if frames were copied incompletely.
        pass


while True:
    now = time.ticks_ms()
    read_usb()
    if time.ticks_diff(now, last_host) > 45000:
        state = "idle"
    for name, button in (("B", button_b),
                         ("UP", button_up), ("DOWN", button_down)):
        pressed = button.is_pressed
        if pressed and not was_pressed[name]:
            if name == "B":
                status_until = time.ticks_add(now, STATUS_TIME_MS)
                shown_mood = None
            elif name == "UP":
                focus_mode = not focus_mode
                status_until = time.ticks_add(now, STATUS_TIME_MS)
                shown_mood = None
            elif name == "DOWN":
                interaction = ("a", "b", "c")[urandom.getrandbits(16) % 3]
                interaction_started = now
                shown_mood = None
        was_pressed[name] = pressed
    draw(now)
    time.sleep_ms(20)
