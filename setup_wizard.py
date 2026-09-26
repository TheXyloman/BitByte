"""Double-clicked, guided setup for BitByte. No executable or administrator rights required."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import messagebox, scrolledtext

import install
from setup_services import (ROOT, SetupError, assert_supported_python, guided_firmware_setup,
                            verify_bundle)


class Wizard:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("BitByte Setup")
        root.geometry("720x520")
        root.minsize(620, 420)
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.busy = False

        tk.Label(root, text="BitByte Setup", font=("TkDefaultFont", 20, "bold")).pack(pady=(18, 4))
        tk.Label(root, text="A guided, user-level setup for Tufty 2040, Codex, and Claude Code.",
                 wraplength=650).pack(padx=20)
        self.message = tk.StringVar(value="Checking this BitByte release…")
        tk.Label(root, textvariable=self.message, anchor="w", justify="left", wraplength=650).pack(fill="x", padx=28, pady=14)

        actions = tk.Frame(root)
        actions.pack(pady=4)
        self.buttons = []
        for label, action in (("Install", self.install), ("Repair", self.repair),
                              ("Firmware Setup", self.firmware), ("Uninstall", self.uninstall)):
            button = tk.Button(actions, text=label, command=action, width=17)
            button.pack(side="left", padx=5)
            self.buttons.append(button)
        self.log = scrolledtext.ScrolledText(root, height=15, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=22, pady=(14, 22))
        self.poll()
        self.prepare()

    def prepare(self):
        try:
            assert_supported_python()
            verify_bundle(ROOT)
        except SetupError as exc:
            self.message.set(str(exc))
            self.write("SETUP BLOCKED: " + str(exc))
            for button in self.buttons:
                button.configure(state="disabled")
            return
        self.message.set("Choose Install for the laptop integration, then Firmware Setup for the Tufty.")
        self.write("Release verified. This unsigned script may require a one-time OS approval to open.")
        self.write("Codex hooks will need to be reviewed/trusted in Codex after setup; BitByte never bypasses that approval.")

    def write(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.write(value)
                elif kind == "done":
                    self.busy = False
                    for button in self.buttons:
                        button.configure(state="normal")
                    self.message.set(value)
                elif kind == "error":
                    self.busy = False
                    for button in self.buttons:
                        button.configure(state="normal")
                    self.message.set(value)
                    self.write("ERROR: " + value)
                    messagebox.showerror("BitByte Setup", value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run(self, title: str, action):
        if self.busy:
            return
        self.busy = True
        self.message.set(title + " is running…")
        for button in self.buttons:
            button.configure(state="disabled")

        def worker():
            try:
                action()
            except (SetupError, ValueError, OSError, SystemExit) as exc:
                self.events.put(("error", str(exc) or title + " failed."))
            except Exception:
                detail = traceback.format_exc()
                self.events.put(("log", detail))
                self.events.put(("error", title + " failed unexpectedly. Copy the diagnostic log when asking for help."))
            else:
                self.events.put(("done", title + " finished successfully."))
        threading.Thread(target=worker, daemon=True).start()

    def status(self, value: str):
        self.events.put(("log", value))
        self.events.put(("log", ""))

    def confirm_flash(self, backup: Path | None) -> bool:
        answer = threading.Event()
        result = {"ok": False}
        def prompt():
            backup_text = ("A backup was saved here:\n" + str(backup) + "\n\n") if backup else (
                "No readable existing filesystem was found, so no backup is possible.\n\n")
            result["ok"] = messagebox.askyesno(
                "Flash Tufty firmware",
                backup_text + "Flashing will erase the Tufty filesystem. Continue only if this is the board you want to use with BitByte.",
                icon="warning")
            answer.set()
        self.root.after(0, prompt)
        answer.wait()
        return result["ok"]

    def host_setup(self, repair: bool = False):
        self.status("Installing BitByte into your user profile; no administrator rights are required.")
        python = install.install_current(bundle=ROOT)
        self.status("Installed bridge runtime with " + str(python))
        self.status("Codex and Claude Code hooks were both configured. Restart either assistant if it was open.")
        if repair:
            self.status("Repair also re-applied startup and hook configuration.")

    def install(self):
        self.run("Install", self.host_setup)

    def repair(self):
        self.run("Repair", lambda: self.host_setup(True))

    def firmware(self):
        def action():
            self.status("Preparing the local bridge before firmware setup.")
            python = install.install_current(bundle=ROOT)
            install.stop_autostart(Path.home())
            try:
                backup_dir = (Path.home() / "Documents" / "BitByte Backups")
                result = guided_firmware_setup(python, backup_dir, self.status, self.confirm_flash)
            finally:
                # Reinstalling recreates and starts the user-level bridge after success or a recoverable failure.
                install.install_autostart(Path.home(), python, None)
            self.status("Tufty verified on " + result.port)
            if result.backup:
                self.status("Backup retained at " + str(result.backup))
        self.run("Firmware Setup", action)

    def uninstall(self):
        def action():
            def prompt():
                if messagebox.askyesno("Remove BitByte", "Remove BitByte hooks and login startup? Firmware and backups will be kept."):
                    confirmation["yes"] = True
                ready.set()
            ready, confirmation = threading.Event(), {"yes": False}
            self.root.after(0, prompt)
            ready.wait()
            if not confirmation["yes"]:
                raise SetupError("Uninstall was cancelled. No changes were made.")
            install.uninstall()
            self.status("Removed BitByte hooks and login startup. Tufty firmware and backups were left untouched.")
        self.run("Uninstall", action)


def main() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("BitByte Setup needs a desktop session. " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    Wizard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
