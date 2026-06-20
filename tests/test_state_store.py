import os
import sys
import tempfile
import gc

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from state_store import StateStore


def test_state_store_round_trip_and_audit_log():
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "guardian.db")
    try:
        store = StateStore(db_path=db_path)

        assert store.get_state("123_2026-06-20") is None

        store.save_state("123_2026-06-20", start_balance=1000.0, peak_equity=1050.0, breaker_tripped=False)
        state = store.get_state("123_2026-06-20")

        assert state["start_balance"] == 1000.0
        assert state["peak_equity"] == 1050.0
        assert state["breaker_tripped"] == 0

        store.log_action("TEST_EVENT", {"foo": "bar"})

        with __import__("sqlite3").connect(db_path) as conn:
            row = conn.execute(
                "SELECT event_type, details FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row[0] == "TEST_EVENT"
            assert '"foo": "bar"' in row[1]
    finally:
        gc.collect()
        try:
            os.remove(db_path)
            os.rmdir(tmp)
        except OSError:
            pass
