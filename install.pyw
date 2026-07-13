"""One-time setup for the EVE Industry Calculator -- no command prompt needed.

Double-click this after installing Python (install.command on macOS,
install.pyw directly on Windows). It installs the required Python packages,
then builds the local EVE data cache and does an initial market price
refresh, showing progress in a small window the whole time. Once it says
"Setup complete," use launch_indycalc.pyw/.command to run the app.

Safe to re-run any time -- reinstalling packages and rebuilding the data
cache are both idempotent.
"""
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "install.log"
IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

STEPS = [
    ("Installing Python packages (streamlit, scipy, pandas, requests)...",
     [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]),
    ("Downloading EVE static data (blueprints, ore, regions -- first time only, ~1-2 min)...",
     [sys.executable, "-m", "indycalc.sde_loader"]),
    ("Fetching initial market prices across the 5 trade hub regions...",
     [sys.executable, "-m", "indycalc.price_cache"]),
]


def log(message: str) -> None:
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def run_steps(status_queue: "queue.Queue[tuple[str, str]]") -> None:
    for label, cmd in STEPS:
        status_queue.put(("status", label))
        log(f"RUN: {' '.join(cmd)}")
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            result = subprocess.run(
                cmd,
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                **({"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}),
            )
        if result.returncode != 0:
            log(f"FAILED (exit {result.returncode}): {' '.join(cmd)}")
            status_queue.put(("error", f"Failed: {label}\nSee install.log for details."))
            return
    status_queue.put(("done", "Setup complete! You can now close this and run launch_indycalc.pyw."))


def main() -> None:
    root = tk.Tk()
    root.title("EVE Industry Calculator -- Setup")
    root.geometry("420x180")
    root.resizable(False, False)

    tk.Label(root, text="EVE Industry Calculator -- Setup", font=("Segoe UI", 12, "bold")).pack(pady=(14, 6))

    status_var = tk.StringVar(value="Starting...")
    status_label = tk.Label(root, textvariable=status_var, wraplength=380, justify="center")
    status_label.pack(pady=6)

    progress = ttk.Progressbar(root, mode="indeterminate", length=340)
    progress.pack(pady=10)
    progress.start(12)

    close_btn = tk.Button(root, text="Close", width=14, state="disabled", command=root.destroy)
    close_btn.pack(pady=6)

    status_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threading.Thread(target=run_steps, args=(status_queue,), daemon=True).start()

    def poll_queue() -> None:
        try:
            kind, message = status_queue.get_nowait()
        except queue.Empty:
            root.after(200, poll_queue)
            return

        if kind == "status":
            status_var.set(message)
            root.after(200, poll_queue)
        elif kind == "done":
            progress.stop()
            progress.config(mode="determinate", value=100)
            status_var.set(message)
            status_label.config(fg="#1a7f37")
            close_btn.config(state="normal")
        elif kind == "error":
            progress.stop()
            status_var.set(message)
            status_label.config(fg="#8a1f11")
            close_btn.config(state="normal")

    root.after(200, poll_queue)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("Installer crashed:\n" + traceback.format_exc())
