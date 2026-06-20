import sqlite3
import os
from datetime import datetime, timezone
import json


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


class StateStore:
    def __init__(self, db_path="state/guardian.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_state (
                    server_day_key TEXT PRIMARY KEY,
                    start_balance REAL,
                    peak_equity REAL,
                    breaker_tripped INTEGER,
                    updated_at TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP,
                    event_type TEXT,
                    details TEXT
                )
            """)

    def get_state(self, server_day_key):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM daily_state WHERE server_day_key = ?",
                (server_day_key,),
            ).fetchone()
            if row:
                return dict(row)
            return None

    def save_state(self, server_day_key, start_balance, peak_equity, breaker_tripped):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_state
                (server_day_key, start_balance, peak_equity, breaker_tripped, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (server_day_key, start_balance, peak_equity, int(breaker_tripped), _utc_now()),
            )

    def log_action(self, event_type, details):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO audit_log (timestamp, event_type, details)
                VALUES (?, ?, ?)
                """,
                (_utc_now(), event_type, json.dumps(details)),
            )
