"""Tests for deploy script logic: Wine version gating and startup log tracking."""

import subprocess
import sys


def wine_supports_crealf_version_check(wine_ver_str: str) -> bool:
    """Python reference equivalent of wine_supports_crealf in install.sh.

    Sorts '10.1' and wine_ver_str using version comparison.
    """
    import re
    m = re.match(r"^wine-([0-9][0-9.]*)", wine_ver_str)
    if not m:
        return False
    ver = m.group(1)

    def parse_ver(v):
        return [int(x) for x in re.findall(r"\d+", v)]

    return parse_ver(ver) >= parse_ver("10.1")


def test_wine_version_gating():
    # Supported versions (>= 10.1)
    assert wine_supports_crealf_version_check("wine-10.1") is True
    assert wine_supports_crealf_version_check("wine-10.1.1") is True
    assert wine_supports_crealf_version_check("wine-10.2") is True
    assert wine_supports_crealf_version_check("wine-11.0") is True

    # Unsupported versions (< 10.1)
    assert wine_supports_crealf_version_check("wine-10.0") is False
    assert wine_supports_crealf_version_check("wine-9.0") is False
    assert wine_supports_crealf_version_check("wine-8.0.2") is False
    assert wine_supports_crealf_version_check("wine-7.0") is False


def test_stale_readiness_lines_ignored(tmp_path):
    """Simulates run.sh tracking of new lines: old lines do not produce false success."""
    console_log = tmp_path / "console.log"

    # Pre-existing console log with an old "Started in " line from a prior run
    console_log.write_text("Started in observe mode (old run)\nError: crashed later\n")

    # Record start lines as done in run.sh
    start_lines = len(console_log.read_text().splitlines())
    assert start_lines == 2

    # A check that looks only at new lines (tail -n +$((start_lines + 1)))
    new_lines = console_log.read_text().splitlines()[start_lines:]
    has_started_new = any("Started in " in line for line in new_lines)
    # Old line must not trigger readiness!
    assert has_started_new is False

    # Now the new process starts and appends its own "Started in " line
    with open(console_log, "a") as f:
        f.write("New session initializing...\nStarted in observe mode\n")

    new_lines_after = console_log.read_text().splitlines()[start_lines:]
    has_started_new_after = any("Started in " in line for line in new_lines_after)
    # New line correctly triggers readiness!
    assert has_started_new_after is True
