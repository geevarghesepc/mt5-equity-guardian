"""Read-only check that this machine can run the guardian. Sends no orders.

python health.py

Reports free memory, CPU, disk, an internet ping, whether the terminal
is connected, the account mode (must be hedging), Algo Trading, the broker
server ping, and managed positions.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from config_loader import load_config  # noqa: E402

PING_OK_MS = 150.0
PING_WARN_MS = 400.0
RAM_OK_GB = 2.0
RAM_FAIL_GB = 0.8
HEARTBEAT_STALE_SEC = 180.0


def grade_ping_ms(ms: float | None) -> str:
    """Trade-server round trip. Above 400 ms, a market order can slip."""
    if ms is None or ms < 0:
        return "fail"
    if ms <= PING_OK_MS:
        return "ok"
    if ms <= PING_WARN_MS:
        return "warn"
    return "fail"


def grade_free_ram_gb(free_gb: float) -> str:
    if free_gb < RAM_FAIL_GB:
        return "fail"
    if free_gb < RAM_OK_GB:
        return "warn"
    return "ok"


def parse_windows_ping(text: str) -> float | None:
    """Average of Windows `ping` replies. `time<1ms` counts as 1."""
    times = []
    for match in re.finditer(r"time[=<](\d+)ms", text, flags=re.IGNORECASE):
        times.append(float(match.group(1)))
    if not times:
        return None
    return sum(times) / len(times)


def machine_snapshot() -> dict:
    """CPU, free memory, and free disk. No extra packages."""
    mem = _memory()
    drive = os.path.splitdrive(ROOT)[0] or ROOT
    disk = shutil.disk_usage(drive or ROOT)
    return {
        "cpu_count": os.cpu_count() or 1,
        "cpu_busy_pct": _cpu_busy_pct(),
        "ram_total_gb": mem[0],
        "ram_free_gb": mem[1],
        "disk_free_gb": disk.free / (1024 ** 3),
        "python": sys.version.split()[0],
    }


def ping_host(host: str, count: int = 4) -> float | None:
    """ICMP average in ms. None when ping is blocked or the host is down."""
    try:
        completed = subprocess.run(
            ["ping", "-n", str(count), "-w", "1000", host],
            capture_output=True,
            text=True,
            timeout=count * 2 + 3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_windows_ping(completed.stdout + completed.stderr)


def heartbeat_age_sec(path: str) -> float | None:
    if not os.path.isfile(path):
        return None
    return time.time() - os.path.getmtime(path)


def _memory() -> tuple[float, float]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0.0, 0.0
    scale = 1024 ** 3
    return status.ullTotalPhys / scale, status.ullAvailPhys / scale


def _cpu_busy_pct(sample_sec: float = 0.4) -> float:
    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", ctypes.c_ulong),
            ("dwHighDateTime", ctypes.c_ulong),
        ]

    def read() -> tuple[int, int]:
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return 0, 0

        def ticks(item: FILETIME) -> int:
            return (int(item.dwHighDateTime) << 32) + int(item.dwLowDateTime)

        idle_t = ticks(idle)
        total = ticks(kernel) + ticks(user)
        return idle_t, total

    idle0, total0 = read()
    time.sleep(sample_sec)
    idle1, total1 = read()
    spent = (total1 - total0) - (idle1 - idle0)
    span = total1 - total0
    if span <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * spent / span))


def _line(label: str, grade: str, detail: str) -> str:
    return f"  {grade.upper():<4}  {label:<28} {detail}"


def check_machine(lines: list[str], fails: list[str], warns: list[str]) -> None:
    snap = machine_snapshot()
    ram_grade = grade_free_ram_gb(snap["ram_free_gb"])
    lines.append(
        _line(
            "memory",
            ram_grade,
            f"{snap['ram_free_gb']:.1f} GB free of {snap['ram_total_gb']:.1f} GB",
        )
    )
    if ram_grade == "fail":
        fails.append("Free memory is under 0.8 GB. MetaTrader and the loop will stall.")
    elif ram_grade == "warn":
        warns.append("Free memory is under 2 GB. Close other programs before trading.")

    cpu_grade = "warn" if snap["cpu_count"] < 2 or snap["cpu_busy_pct"] >= 85 else "ok"
    lines.append(
        _line(
            "cpu",
            cpu_grade,
            f"{snap['cpu_count']} cores, {snap['cpu_busy_pct']:.0f}% busy, Python {snap['python']}",
        )
    )
    if snap["cpu_count"] < 2:
        warns.append("One CPU core is tight for MetaTrader + guardian loop.")
    elif snap["cpu_busy_pct"] >= 85:
        warns.append("The CPU is already above 85%. The loop may lag.")

    disk_grade = "warn" if snap["disk_free_gb"] < 2 else "ok"
    lines.append(_line("disk", disk_grade, f"{snap['disk_free_gb']:.1f} GB free"))
    if disk_grade == "warn":
        warns.append("Under 2 GB free on this drive. Logs and MetaTrader history need room.")


def check_network(lines: list[str], warns: list[str]) -> None:
    internet = ping_host("1.1.1.1")
    if internet is None:
        lines.append(_line("internet ping", "warn", "no reply from 1.1.1.1"))
        warns.append("ICMP to 1.1.1.1 failed. The trade-server ping below is the execution number.")
        return
    grade = "ok" if internet <= PING_WARN_MS else "warn"
    lines.append(_line("internet ping", grade, f"{internet:.0f} ms average to 1.1.1.1"))
    if grade == "warn":
        warns.append("The public path is slow. Confirm the trade-server ping.")


def check_terminal(cfg: dict, lines: list[str], fails: list[str], warns: list[str]) -> None:
    import MetaTrader5 as mt5

    started = time.perf_counter()
    if not mt5.initialize():
        lines.append(_line("terminal", "fail", f"initialize failed: {mt5.last_error()}"))
        fails.append("MetaTrader is not connected. Open the terminal and log in.")
        return
    lines.append(_line("terminal", "ok", f"API attached in {(time.perf_counter() - started) * 1000:.0f} ms"))
    try:
        _terminal_details(mt5, cfg, lines, fails, warns)
    finally:
        mt5.shutdown()


def _terminal_details(mt5, cfg: dict, lines: list[str], fails: list[str], warns: list[str]) -> None:
    info = mt5.terminal_info()
    account = mt5.account_info()
    if info is None or account is None:
        lines.append(_line("account", "fail", f"no terminal info: {mt5.last_error()}"))
        fails.append("The terminal has no account. Log in, then run this again.")
        return

    connected = bool(getattr(info, "connected", False))
    trade_allowed = bool(getattr(info, "trade_allowed", False))
    lines.append(
        _line(
            "session",
            "ok" if connected else "fail",
            f"{'connected' if connected else 'disconnected'}, build {getattr(info, 'build', '')}",
        )
    )
    if not connected:
        fails.append("The terminal is open but not connected to the trade server.")

    # Account mode — must be hedging for the two-position policy.
    margin_mode = int(getattr(account, "margin_mode", 0))
    HEDGING = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
    if margin_mode == HEDGING:
        lines.append(_line("margin mode", "ok", "hedging"))
    else:
        lines.append(_line("margin mode", "fail", f"mode {margin_mode} (not hedging)"))
        fails.append(
            f"Account margin mode is {margin_mode}, not hedging ({HEDGING}). "
            "The two-position policy requires a hedging account."
        )

    # Trade mode.
    trade_mode = int(getattr(account, "trade_mode", -1))
    REAL = int(getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 0))
    DEMO = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 1))
    CONTEST = int(getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 2))
    mode_names = {REAL: "real", DEMO: "demo", CONTEST: "contest"}
    mode_name = mode_names.get(trade_mode, f"unknown({trade_mode})")
    detail = f"#{int(account.login)} {account.server} {mode_name}  balance {float(account.balance):.2f}"
    lines.append(_line("account", "ok" if mode_name in ("demo", "contest", "real") else "warn", detail))

    # Margin level.
    margin = float(getattr(account, "margin", 0) or 0)
    level = float(getattr(account, "margin_level", 0) or 0)
    if margin <= 0:
        lines.append(_line("margin", "ok", "no margin in use"))
    elif level < 100:
        lines.append(_line("margin", "fail", f"level {level:.0f}%"))
        fails.append(f"Margin level is {level:.0f}%. The account can be stopped out.")
    elif level < 200:
        lines.append(_line("margin", "warn", f"level {level:.0f}%"))
        warns.append(f"Margin level is {level:.0f}%.")
    else:
        lines.append(_line("margin", "ok", f"level {level:.0f}%"))

    # Algo Trading.
    order_grade = "ok" if trade_allowed and not bool(getattr(info, "tradeapi_disabled", False)) else "fail"
    lines.append(
        _line(
            "orders",
            order_grade,
            "Algo Trading on" if order_grade == "ok" else "Algo Trading is off",
        )
    )
    if order_grade == "fail":
        fails.append("Turn on Algo Trading in the terminal. Orders cannot be sent.")

    # Trade-server ping.
    ping_us = getattr(info, "ping_last", None)
    ping_ms = None if ping_us in (None, 0) else float(ping_us) / 1000.0
    ping_grade = "warn" if ping_ms is None else grade_ping_ms(ping_ms)
    detail = "not measured yet" if ping_ms is None else f"{ping_ms:.0f} ms to the trade server"
    lines.append(_line("trade ping", ping_grade, detail))
    if ping_ms is None:
        warns.append("The terminal has not reported a trade-server ping yet.")
    elif ping_grade == "warn":
        warns.append(f"Trade-server ping is {ping_ms:.0f} ms. Fills still work. Slippage can be wider.")
    elif ping_grade == "fail":
        fails.append(f"Trade-server ping is {ping_ms:.0f} ms. A market order is likely to slip.")

    # Managed positions summary.
    from mt5_executor import MT5Executor

    executor = MT5Executor(cfg)
    executor.connected = True
    positions = executor.get_positions()
    if positions:
        lines.append(
            _line("positions", "ok", f"{len(positions)} managed position(s)")
        )
        for p in positions:
            lines.append(
                f"         ticket {p['ticket']}  {p['symbol']}  "
                f"vol={p['volume']}  profit={p.get('profit', 0):.2f}  "
                f"sl={p.get('sl', 0)}"
            )
    else:
        lines.append(_line("positions", "ok", "no managed positions"))

    # Guardian heartbeat.
    log_path = os.path.join(ROOT, "logs", "guardian.log")
    age = heartbeat_age_sec(log_path)
    if age is None:
        lines.append(_line("guardian log", "warn", "no log file"))
        warns.append("No guardian log file found at logs/guardian.log. The guardian has not run yet.")
    else:
        fresh = age <= HEARTBEAT_STALE_SEC
        if fresh:
            lines.append(_line("guardian log", "ok", f"modified {age:.0f}s ago"))
        else:
            lines.append(
                _line(
                    "guardian log",
                    "warn",
                    f"modified {age / 60:.0f} min ago, loop looks stopped",
                )
            )
            warns.append(
                f"Guardian log has not been updated in {age / 60:.1f} minutes "
                f"(threshold {HEARTBEAT_STALE_SEC:.0f}s). The guardian loop appears stopped."
            )


def main() -> int:
    cfg = load_config(os.path.join(ROOT, "config.yaml")) or {}
    lines = ["Machine"]
    fails: list[str] = []
    warns: list[str] = []
    check_machine(lines, fails, warns)
    lines.append("Network")
    check_network(lines, warns)
    lines.append("MetaTrader")
    check_terminal(cfg, lines, fails, warns)
    print("\n".join(lines))
    print()
    if fails:
        print("Stop.")
        for item in fails:
            print(f"  {item}")
    elif warns:
        print("Usable, with warnings.")
        for item in warns:
            print(f"  {item}")
    else:
        print("Ready. The machine, the terminal, and the guardian are inside the limits.")
    if warns and fails:
        for item in warns:
            print(f"  {item}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
