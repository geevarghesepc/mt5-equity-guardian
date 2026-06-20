import os
import time
import logging
from datetime import datetime, timezone
import datetime as dt
import MetaTrader5 as mt5

from config_loader import load_config, replace_config
from logging_setup import setup_logging
from mt5_executor import MT5Executor
from risk_engine import update_peak_equity, trailing_threshold, daily_drawdown_breached
from anti_averaging import evaluate as evaluate_anti_avg
from state_store import StateStore
from notifications import Notifier

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 300


def get_server_day_key(account_login, reset_hour=0, reference_symbol="EURUSD"):
    tick = mt5.symbol_info_tick(reference_symbol)
    if tick:
        server_time = datetime.utcfromtimestamp(tick.time)
    else:
        server_time = datetime.now(timezone.utc).replace(tzinfo=None)

    if server_time.hour < reset_hour:
        logical_date = server_time - dt.timedelta(days=1)
    else:
        logical_date = server_time

    return f"{account_login}_{logical_date.strftime('%Y-%m-%d')}"


def apply_runtime_config(config, executor, notifier):
    """Hot-reload all config-driven settings."""
    account_changed = executor.apply_config(config)
    notifier.apply_config(config)

    loop_cfg = config.get("loop", {})
    server_cfg = config.get("server", {})
    execution_cfg = config.get("execution", {})
    cooldown_cfg = config.get("cooldown", {})

    loop_interval = float(loop_cfg.get("interval_ms", 200)) / 1000.0
    reset_hour = int(server_cfg.get("day_reset_hour", 0))
    server_time_symbol = execution_cfg.get("server_time_symbol", "EURUSD")
    cooldown_sec = float(cooldown_cfg.get("after_stop_sec", 0))

    return loop_interval, reset_hour, server_time_symbol, cooldown_sec, account_changed


def reload_config(config):
    """Hot-reload config; keep last-known-good values on parse/read errors."""
    try:
        new_config = load_config()
    except Exception as exc:
        logger.error("Config hot-reload failed; keeping previous config: %s", exc)
        return False

    replace_config(config, new_config)
    return True


def ensure_mt5_connection(executor, notifier, mt5_online):
    """Fetch account info, reconnect on failure, and emit connection alerts."""
    account = executor.get_account()
    if account:
        if not mt5_online:
            logger.info("MT5 connection restored.")
            notifier.send("✅ MT5 Guardian connection restored.")
        return account, True

    if mt5_online:
        logger.error("MT5 connection lost.")
        notifier.send("🚨 MT5 Guardian connection lost! Attempting reconnect...")

    if not executor.reconnect():
        return None, False

    account = executor.get_account()
    if account:
        logger.info("MT5 connection restored after reconnect.")
        notifier.send("✅ MT5 Guardian connection restored.")
        return account, True

    return None, False


def run_iteration(config, executor, store, notifier, mt5_online, cooldown_until):
    """Run one guardian loop iteration. Returns (mt5_online, cooldown_until)."""
    if not reload_config(config):
        return mt5_online, cooldown_until

    loop_interval, reset_hour, server_time_symbol, cooldown_sec, account_changed = apply_runtime_config(
        config, executor, notifier
    )

    if account_changed:
        logger.info("Account credentials changed in config; reconnecting.")
        if not executor.reconnect():
            notifier.send("🚨 MT5 Guardian failed to reconnect after account config change.")
            time.sleep(1)
            return mt5_online, cooldown_until

    account, mt5_online = ensure_mt5_connection(executor, notifier, mt5_online)
    if not account:
        time.sleep(1)
        return mt5_online, cooldown_until

    now = time.time()
    if cooldown_until and now < cooldown_until:
        time.sleep(loop_interval)
        return mt5_online, cooldown_until

    equity = float(account["equity"])
    balance = float(account["balance"])

    positions = executor.get_positions()
    pos_count = len(positions)

    for p in positions:
        executor.ensure_stop_loss(p, equity, pos_count)

    day_key = get_server_day_key(account["login"], reset_hour, server_time_symbol)
    state = store.get_state(day_key)

    if not state:
        logger.info("New day detected: %s. Resetting state.", day_key)
        store.save_state(day_key, balance, equity, 0)
        state = store.get_state(day_key)

    start_balance = float(state["start_balance"])
    peak_equity = float(state["peak_equity"])
    breaker_tripped = bool(state["breaker_tripped"])

    if pos_count > 0:
        new_peak = update_peak_equity(peak_equity, equity)
        if new_peak > peak_equity:
            store.save_state(day_key, start_balance, new_peak, breaker_tripped)
            peak_equity = new_peak
    else:
        if peak_equity != balance:
            store.save_state(day_key, start_balance, balance, breaker_tripped)
            peak_equity = balance

    if not breaker_tripped:
        if daily_drawdown_breached(start_balance, equity, config):
            msg = (
                f"⛔ DAILY CIRCUIT BREAKER TRIPPED! Equity dropped to {equity} "
                f"(Start: {start_balance})"
            )
            logger.critical(msg)
            store.save_state(day_key, start_balance, peak_equity, 1)
            store.log_action("CIRCUIT_BREAKER", {"equity": equity, "start": start_balance})
            if pos_count > 0:
                logger.warning("Breaker tripped but positions still open. Closing all.")
                executor.close_all("Daily Circuit Breaker")
            notifier.send(msg)
            if cooldown_sec > 0:
                cooldown_until = time.time() + cooldown_sec

    state = store.get_state(day_key)
    breaker_tripped = bool(state["breaker_tripped"])

    if breaker_tripped:
        if pos_count > 0:
            logger.warning("Breaker tripped but positions still open. Closing all.")
            executor.close_all("Daily Circuit Breaker")
        time.sleep(loop_interval)
        return mt5_online, cooldown_until

    tickets_to_flatten = evaluate_anti_avg(positions, config)
    for t in tickets_to_flatten:
        msg = f"⚠️ Anti-Averaging Flatten: Ticket {t['ticket']} ({t['symbol']})"
        logger.warning(msg)
        if executor.close_ticket(t):
            store.log_action("ANTI_AVG_FLATTEN", t)
            notifier.send(msg)

    if tickets_to_flatten:
        positions = executor.get_positions()
        pos_count = len(positions)
        refreshed_account = executor.get_account()
        if refreshed_account:
            equity = float(refreshed_account["equity"])

    threshold = trailing_threshold(peak_equity, start_balance, pos_count, config)
    if pos_count > 0 and equity <= threshold:
        msg = f"💥 TRAILING STOP HIT! Equity {equity} <= {threshold} (Peak: {peak_equity})"
        logger.critical(msg)
        executor.close_all("Trailing Equity Stop")
        store.log_action(
            "TRAILING_STOP",
            {"equity": equity, "threshold": threshold, "peak": peak_equity},
        )
        notifier.send(msg)
        if cooldown_sec > 0:
            cooldown_until = time.time() + cooldown_sec
        time.sleep(1)

    time.sleep(loop_interval)
    return mt5_online, cooldown_until


def run():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    os.chdir(project_root)

    setup_logging()
    logger.info("Logging initialized.")

    config = load_config()
    executor = MT5Executor(config)
    store = StateStore()
    notifier = Notifier(config)

    if not executor.connect():
        notifier.send("🚨 MT5 Guardian failed to connect!")
        notifier.shutdown()
        return

    notifier.send(f"🛡️ MT5 Guardian started. Mode: {executor.mode.upper()}")
    logger.info("Started in %s mode.", executor.mode.upper())

    mt5_online = True
    cooldown_until = 0.0
    last_heartbeat = time.time()
    consecutive_errors = 0

    try:
        while True:
            try:
                mt5_online, cooldown_until = run_iteration(
                    config, executor, store, notifier, mt5_online, cooldown_until
                )
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                logger.exception("Guardian loop error: %s", exc)
                store.log_action("LOOP_ERROR", {"error": str(exc), "count": consecutive_errors})
                if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                    notifier.send(f"🚨 MT5 Guardian loop error ({consecutive_errors}x): {exc}")
                time.sleep(1)

            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                account = executor.get_account()
                if account:
                    logger.info(
                        "Heartbeat: account=%s equity=%s balance=%s mode=%s",
                        account.get("login"),
                        account.get("equity"),
                        account.get("balance"),
                        executor.mode,
                    )
                else:
                    logger.warning("Heartbeat: MT5 account unavailable.")
                last_heartbeat = now

    except KeyboardInterrupt:
        logger.info("Guardian Bot shutting down gracefully.")
        notifier.send("🛡️ MT5 Guardian stopped.")
    finally:
        notifier.shutdown()
        mt5.shutdown()


if __name__ == "__main__":
    run()
