#!/bin/bash
# macOS double-click entry point for one-time setup.
# Finder runs .command files in a new Terminal window; this just hands off
# to install.pyw (a normal Python source file despite the extension -- .pyw
# is a Windows file-association convention the interpreter ignores) as a
# detached background process, then lets the Terminal window close itself.
cd "$(dirname "$0")"
nohup python3 install.pyw >/dev/null 2>&1 &
disown
sleep 1
