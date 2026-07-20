#!/bin/bash
# macOS double-click entry point for running the app day-to-day.
# Finder runs .command files in a new Terminal window; this just hands off
# to launch_indycalc.pyw (a normal Python source file despite the extension
# -- .pyw is a Windows file-association convention the interpreter ignores)
# as a detached background process, then lets the Terminal window close
# itself. The launcher's own control window (Tkinter) stays open in the
# Dock; closing it stops the server, same as on Windows.
cd "$(dirname "$0")"
nohup python3 launch_indycalc.pyw >/dev/null 2>&1 &
disown
sleep 1
