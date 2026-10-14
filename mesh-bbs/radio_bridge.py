"""
radio_bridge.py

Common interface implemented by both radio backends (meshtastic_bridge.py for
Meshtastic, meshcore_bridge.py for MeshCore), plus the shared SQLite
message log they both write to/read from. modem_bbs.py only talks to this
interface, so it doesn't need to know which radio is actually attached.
"""

import sqlite3
import threading
from datetime import datetime


class RadioBridge:
    """Abstract interface. Both backends implement these three methods."""

    def get_recent(self, limit=10):
        """Return oldest-first list of (timestamp, sender, direction, text)."""
        raise NotImplementedError

    def send(self, text, destination=None):
        """destination=None means broadcast; otherwise backend-specific
        (Meshtastic: node ID. MeshCore: contact name or pubkey prefix)."""
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class MessageLog:
    """Thread-safe SQLite log shared by whichever backend is active."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                sender TEXT,
                direction TEXT,   -- 'in' or 'out'
                text TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def add(self, sender, direction, text):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO messages (timestamp, sender, direction, text) "
                "VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), sender, direction, text),
            )
            conn.commit()
            conn.close()

    def recent(self, limit=10):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT timestamp, sender, direction, text FROM messages "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return list(reversed(rows))
