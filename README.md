# BitByte

A small, local, animated Tufty 2040 companion for Codex and Claude Code. The laptop receives lifecycle events through hooks and sends only a mood to the Tufty over USB serial. No account, API key, cloud service, or model call is needed.

## What it does

| Agent event | Tufty reaction |
| --- | --- |
| Prompt or tool activity | Blue, bobbing and thinking |
| Supported attention notification | Yellow, surprised and asking for you |
| Permission request | Yellow, surprised and asking for you |
| Tool or API failure | Pink, briefly confused |
| Turn completed | Purple celebration |
| No activity | Teal, blinking and resting |

The front buttons are deliberately simple:

| Button | Action |
| --- | --- |
| **A** | Approves the currently displayed Codex or Claude Code permission request. |
| **B** | Shows link, mood, and focus status for three seconds (except during approval). |
| **C** | Rejects the currently displayed permission request. |
| **Up** | Toggles focus mode: a visibly dimmer screen and slower idle/working animation; shows a confirmation card. |
| **Down** | Plays one of three randomly selected interaction animations. |

The approval button path waits up to 75 seconds. If no button is pressed, the coding assistant shows its usual approval UI. A and C have no effect unless the pet is visibly asking for approval.

The bridge handles concurrent Codex and Claude Code sessions. Permission requests take priority, then supported attention notifications, then errors and work. The Tufty returns to idle if it stops hearing from the laptop. Focus mode resets when the Tufty restarts; approval, attention, errors, completion, and interactions always use normal brightness and animation speed.

Codex desktop plan questions and `requestUserInput` waits are not hook events, so they do not automatically show Attention in this hook-only version. Claude Code's supported `idle_prompt` and `elicitation_dialog` notifications do show Attention. A and C never answer ordinary questions; they only act on a live permission request.

## Set up on macOS or Windows

1. Connect the **Tufty 2040** with a data-capable USB cable. Install Pimoroni's [Tufty 2040 MicroPython UF2](https://github.com/pimoroni/pimoroni-pico/releases) if the board does not already run the Pimoroni MicroPython build. Their [setup guide](https://github.com/pimoroni/pimoroni-pico/blob/main/setting-up-micropython.md) explains BOOTSEL mode and the correct UF2.
2. Copy the contents of [`firmware/`](firmware/) onto the Tufty using [Thonny](https://thonny.org/): `main.py` belongs at the board's top level and both the `frames/` and `interactions/` folders must stay beside it. Save any existing `main.py` first if you want to restore the demo. Restart the board. Its screen should show the resting animation.
3. From this directory, run `python3 install.py` on macOS, or `py -3 install.py` on Windows. This copies the runtime into your user application-data folder, creates its `.venv`, installs `pyserial`, adds user-level hooks for both assistants, and sets the bridge to start at login. Existing Claude Code hooks are preserved and its settings are backed up.
4. In Codex, run `/hooks` and review/trust the new hooks. Restart Codex and Claude Code if either was open during setup.
5. Submit a prompt in Codex or Claude Code. The Tufty should turn blue. To cycle through example reactions, run `python3 bridge.py --demo` on macOS or `py -3 bridge.py --demo` on Windows.

**If the badge does not connect:** close Thonny, which may have the USB serial port open. The bridge tests candidate serial ports with a `HELLO` handshake and accepts only this firmware. If needed, rerun `install.py --port /dev/cu.usbmodem...` on macOS or `install.py --port COM3` on Windows. The macOS bridge log is `~/Library/Application Support/tufty-ai-pet/bridge.log`.

## Manual checks

- Unplug and reconnect the Tufty: it should return to the current activity or idle within a few seconds.
- Try a Codex and a Claude Code session at once: an approval request should take priority over Attention and work.
- Press B while idle or working: a three-second status card should show `CONNECTED`, the current mood, and focus mode. Press Up and confirm idle/working becomes dimmer and slower; attention and approval stay bright.
- Restart the laptop and confirm the bridge returns without a terminal window.
- Run `python3 -m unittest discover -s tests -v` to check event mapping and hook preservation.

## Privacy and behavior

The hook adapters read each hook payload but write only `source`, `session`, state, and a short-lived random approval identifier to user-owned files in the system temp directory. They do not store or send prompt text, response text, file contents, commands, or tool output. The serial protocol sends only `HELLO`, `S <mood>`, and physical button messages. No network connection is used.

For a permission request, the approval hook waits for a physical A or C press and returns that decision to the coding assistant. If the bridge is closed, the Tufty is disconnected, or the 75-second window expires, it makes no decision and the coding assistant uses its normal approval UI.

## Files

- `firmware/main.py`: Tufty display, animation, button, and USB protocol.
- `firmware/frames/`: 30 supplied, device-sized JPEG frames. They map `Idle`, `Thinking`, `Attention`, `Error`, and `Completed` to the pet's resting, working, attention/approval, error, and completion states.
- `firmware/interactions/`: 18 device-sized JPEG frames across three random Down-button interactions.
- `hook.py`: fast content-free agent hook adapter.
- `approval_hook.py`: content-free, time-limited physical approval adapter.
- `bridge.py`: local event receiver and USB reconnect loop.
- `pet.py`: common event mapping and concurrent session state.
- `install.py`: user-level hook and login startup installer.
