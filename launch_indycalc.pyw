"""Double-click launcher for the EVE Industry Calculator -- no console window.

Starts the Streamlit server in the background (if not already running),
opens it in your default browser, and shows a small always-on-top-free
control window in the taskbar so you have a normal way to stop the server
(button, or just close the window) instead of having to hunt it down in
Task Manager or run stop_indycalc.pyw.
"""
import subprocess
import sys
import time
import tkinter as tk
import traceback
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "indycalc" / "app.py"
LOG_PATH = ROOT / "launcher.log"
SERVER_LOG_PATH = ROOT / "streamlit_server.log"
PID_PATH = ROOT / "indycalc.pid"
PORT = 8501

CREATE_NO_WINDOW = 0x08000000


def log(message: str) -> None:
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def is_running() -> bool:
    try:
        urllib.request.urlopen(f"http://localhost:{PORT}", timeout=1)
        return True
    except Exception:
        return False


def start_server() -> None:
    log_file = open(SERVER_LOG_PATH, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", str(APP),
            "--server.headless", "true",
            "--server.port", str(PORT),
        ],
        stdout=log_file,
        stderr=log_file,
        creationflags=CREATE_NO_WINDOW,
        cwd=str(ROOT),
    )
    PID_PATH.write_text(str(proc.pid))
    log(f"Started Streamlit server, pid={proc.pid}")

    for _ in range(30):  # wait up to ~15s for it to come up
        if is_running():
            return
        time.sleep(0.5)
    log("Warning: server did not respond within 15s, opening browser anyway")


def stop_server() -> None:
    if not PID_PATH.exists():
        return
    pid = PID_PATH.read_text().strip()
    subprocess.run(
        ["taskkill", "/PID", pid, "/F", "/T"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    PID_PATH.unlink(missing_ok=True)
    log(f"Stopped server, pid={pid}")


def show_control_window() -> None:
    root = tk.Tk()
    root.title("EVE Industry Calculator")
    root.geometry("340x160")
    root.resizable(False, False)

    tk.Label(root, text="EVE Industry Calculator", font=("Segoe UI", 12, "bold")).pack(pady=(14, 2))
    status_var = tk.StringVar(value=f"Running at localhost:{PORT}")
    status_label = tk.Label(root, textvariable=status_var, fg="#1a7f37")
    status_label.pack(pady=2)

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=12)

    def do_open() -> None:
        webbrowser.open(f"http://localhost:{PORT}")

    def do_stop() -> None:
        stop_server()
        status_var.set("Stopped.")
        status_label.config(fg="#8a1f11")
        open_btn.config(state="disabled")
        stop_btn.config(state="disabled")
        root.after(700, root.destroy)

    open_btn = tk.Button(btn_frame, text="Open in Browser", width=16, command=do_open)
    open_btn.grid(row=0, column=0, padx=6)
    stop_btn = tk.Button(btn_frame, text="Stop Server", width=16, command=do_stop, fg="#8a1f11")
    stop_btn.grid(row=0, column=1, padx=6)

    tk.Label(root, text="Closing this window also stops the server.", fg="gray").pack(pady=(4, 0))

    root.protocol("WM_DELETE_WINDOW", do_stop)
    root.mainloop()


def main() -> None:
    if is_running():
        # Someone else's control window is presumably already managing the
        # running server -- just reopen the browser instead of spawning a
        # second control window that would fight over the same process.
        webbrowser.open(f"http://localhost:{PORT}")
        return
    start_server()
    webbrowser.open(f"http://localhost:{PORT}")
    show_control_window()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("Launcher crashed:\n" + traceback.format_exc())
