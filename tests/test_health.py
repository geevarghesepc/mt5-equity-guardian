"""Thresholds for the read-only health command. No terminal required."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from health import grade_free_ram_gb, grade_ping_ms, parse_windows_ping


def test_trade_ping_bands():
    assert grade_ping_ms(40) == "ok"
    assert grade_ping_ms(150) == "ok"
    assert grade_ping_ms(220) == "warn"
    assert grade_ping_ms(400) == "warn"
    assert grade_ping_ms(900) == "fail"
    assert grade_ping_ms(None) == "fail"


def test_memory_budgets():
    assert grade_free_ram_gb(8) == "ok"
    assert grade_free_ram_gb(1.2) == "warn"
    assert grade_free_ram_gb(0.4) == "fail"


    assert parse_windows_ping("Request timed out.") is None


def test_heartbeat_age_sec_missing_file():
    from health import heartbeat_age_sec
    assert heartbeat_age_sec("non_existent_file_path.log") is None


def test_heartbeat_age_sec_existing_file(tmp_path):
    from health import heartbeat_age_sec
    import time
    log_file = tmp_path / "guardian.log"
    log_file.write_text("Started in observe mode\n")
    age = heartbeat_age_sec(str(log_file))
    assert age is not None
    assert 0 <= age < 5.0


def test_health_stale_log_demotes_to_warn(tmp_path):
    import time
    from unittest.mock import patch
    import health

    lines = []
    warns = []
    log_file = tmp_path / "guardian.log"
    log_file.write_text("test")

    # Simulate log file modified 300 seconds ago (> 180s HEARTBEAT_STALE_SEC)
    stale_mtime = time.time() - 300.0
    os.utime(str(log_file), (stale_mtime, stale_mtime))

    with patch("health.ROOT", str(tmp_path)):
        os.makedirs(tmp_path / "logs", exist_ok=True)
        stale_log = tmp_path / "logs" / "guardian.log"
        stale_log.write_text("test")
        os.utime(str(stale_log), (stale_mtime, stale_mtime))

        age = health.heartbeat_age_sec(str(stale_log))
        assert age is not None and age >= 300.0
        assert age > health.HEARTBEAT_STALE_SEC

