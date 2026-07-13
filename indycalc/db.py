"""Shared SQLite connection helper.

Streamlit can run more than one script execution concurrently in the same
process (multiple browser tabs/sessions, or an interaction firing a rerun
before the previous run's slow network-bound price fetch finished writing).
Plain sqlite3.connect() defaults to a 0ms busy timeout, so any overlap raises
"database is locked" immediately. WAL mode plus a real busy timeout lets a
connection wait instead of failing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 30_000


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn
