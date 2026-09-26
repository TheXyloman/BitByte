# BitByte

A local, animated Tufty 2040 companion for Codex and Claude Code. Hooks and a local Codex session observer track activity; the laptop sends only a mood over USB. No account, API key, network service, or model call is needed.

## Reactions and controls

| Activity | Tufty reaction |
| --- | --- |
| Prompt or tool activity | Blue thinking animation |
| Question, proposed plan, permission, or elicitation requiring input | Yellow attention animation |
| Tool or API failure | Brief pink error animation |
| Normal completed response | Brief purple celebration, then idle |
| No tracked work or disconnected host | Teal resting animation |

| Button | Action |
| --- | --- |
| **A / C** | Disabled; never approve, reject, or cancel assistant actions |
| **B** | Shows link, mood, and focus status for three seconds |
| **Up** | Toggles a dimmer screen and slower idle/working animation |
| **Down** | Plays one of three random interaction animations |

Answer questions and permissions in Codex or Claude normally. Hooks report attention immediately without delaying the assistant's permission UI. Older firmware's A/C messages are ignored by the updated bridge.

Attention takes priority over errors, work, and completion across sessions. A matching response resolves a structured request; unrelated tools and other sessions cannot clear it. A new prompt clears the previous turn's requests. Final questions and plans stay yellow until the next prompt or session termination. Working and attention have no arbitrary inactivity timeout. Completion and error animations last five seconds. The firmware falls back to idle after 45 seconds without a host update.

## Detection and limitations

- Both assistants use `PreToolUse` to detect question tools. Claude's `AskUserQuestion`, `ExitPlanMode`, permissions, and MCP elicitations are supported.
- Codex's local session observer follows new records to catch user-input tools and final proposed plans even where hooks are absent. It correlates calls with results. The immediate acknowledgement of an asynchronous question does not mean the question was answered.
- Final reply text is checked locally for proposed-plan markup and explicit closing requests. Quoted/code examples and optional offers are excluded. Ordinary-language detection is conservative and can miss unusual wording.
- Claude's `idle_prompt` alone does not trigger attention: it also occurs after ordinary completed replies.
- Codex transcript formats are not a stable API. Unknown record types are ignored. Existing session files are followed from their end on the first run, so old conversations are not replayed. Once running, metadata checkpoints preserve pending requests and file positions across restarts. Start a new turn after first installation to establish tracking.
- A crashed assistant that emits no termination event may leave a tracked request or working state. Silence is not treated as proof of completion. A new prompt or session termination clears the old request.

## Set up or upgrade on macOS and Windows

1. Connect a **Tufty 2040** using a data-capable USB cable. If required, install Pimoroni's [Tufty MicroPython UF2](https://github.com/pimoroni/pimoroni-pico/releases) using their [setup guide](https://github.com/pimoroni/pimoroni-pico/blob/main/setting-up-micropython.md).
2. Copy `firmware/main.py`, `firmware/frames/`, and `firmware/interactions/` to the board using Thonny. Back up the board's existing `main.py` first, then restart it. Close Thonny when finished so it releases the serial port.
3. Run `python3 install.py` on macOS or `py -3 install.py` on Windows. The installer updates the user runtime, hooks, and login startup. It replaces the old blocking approval adapter with a non-blocking compatibility entrypoint for assistants that have cached the old command, and preserves unrelated hooks, including handlers sharing a group. The compatibility entrypoint only reports attention; it cannot approve or reject anything.
4. Review/trust the changed Codex hooks using `/hooks`. Restart Codex and Claude Code if needed to reload hook settings.
5. Submit a prompt. Run `python3 bridge.py --demo` (Windows: `py -3 bridge.py --demo`) to send example moods to the running bridge.

### Plug-and-play USB

Startup automatically discovers the device. Move the Tufty to another USB port or data-capable hub without reinstalling or restarting BitByte. The bridge re-enumerates available ports, checks the firmware's `HELLO` / `TUFTY_PET/1` handshake, and sends the current mood after reconnecting. Reconnection usually takes a few seconds; busy or nonmatching candidates can add handshake time.

Discovery prefers a previously verified USB serial number when available, then Tufty/RP2040 candidates, then other USB serial devices. With multiple compatible devices, the current connection is retained; otherwise a stable port ordering breaks ties. Other USB serial candidates receive only the identification handshake until verified.

For troubleshooting, `bridge.py --port PORT` tries that port first and still falls back to discovery. Existing startup configurations with `--port` also gain this fallback. Reinstalling removes saved port arguments from macOS and Windows startup; `install.py --port` is retained for compatibility but no longer pins startup.

If the device is busy, close Thonny or another serial terminal. The bridge keeps retrying automatically. Discovery occurs in a background worker so slow ports do not block activity tracking.

## Diagnostics and privacy

The macOS runtime is `~/Library/Application Support/tufty-ai-pet`; Windows uses `%LOCALAPPDATA%\tufty-ai-pet`. `bridge.log` rotates at 512 KiB with two backups. It contains mood transitions, attention reasons, and connection status, never conversation content.

Hooks read their payloads in memory. The observer reads local Codex session records in memory. BitByte saves only source/session/turn/request identifiers, timestamps, tool names, bounded reasons, transcript paths, file offsets, and minimal session state. It does not save prompt text, response text, commands, arguments, answers, or tool output. `state.json` stores restart checkpoints in the user runtime; the event stream lives in the system temporary directory. USB carries only handshake and mood messages. No network connection is used during operation.

## Validation

Run `python3 -m unittest discover -s tests -v`.

Manual checks:

- Ask a question and produce a plan in both assistants: attention should persist until answered or continued.
- Leave a request open for more than ten minutes; run work longer than two minutes.
- Run simultaneous sessions: unrelated work must not erase attention.
- Confirm ordinary completed responses celebrate, then rest.
- Press A/C during a permission request: no decision should be sent. B/Up/Down retain their local functions.
- Unplug/reconnect through a different USB port or hub; confirm the current mood returns automatically.
- Start the bridge without the Tufty, then connect it. Repeat after restarting the laptop.
- Repeat on Windows to verify COM-number changes and login startup.

## Files

- `pet.py`: event classification and concurrent session/request state.
- `hook.py`: fast non-blocking event adapter.
- `observer.py`: bounded JSONL reader and Codex session adapter.
- `bridge.py`: event processing, checkpoints, diagnostics, and USB recovery.
- `install.py`: hook migration and login startup installation.
- `firmware/`: display firmware and existing animation assets.
- `tests/`: event, state, observer, installer, and USB regression tests.
