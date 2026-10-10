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
from anti_averaging import PositionPolicy
from state_store import StateStore
from notifications import Notifier

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 60


def get_server_day_key(account_login, reset_hour=0, reference_symbol="EURUSD"):
    tick = mt5.symbol_info_tick(reference_symbol)
    if tick:
        server_time = datetime.fromtimestamp(tick.time, timezone.utc).replace(tzinfo=None)
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


def _close_policy_tickets(executor, store, notifier, policy, tickets_to_close):
    """Close tickets decided by the position policy.  Report results back."""
    closed_count = 0
    for t in tickets_to_close:
        ticket = int(t["ticket"])
        symbol = t["symbol"]
        profit = t.get("profit", 0)

        # Determine reason for the close message.
        decision = policy.decided_tickets.get(ticket)
        if decision == "close":
            reason_label = "Same-symbol loser"
            reason_code = "SAME_SYMBOL_CLOSE"
        elif ticket in policy.pending_closes:
            reason_label = "Retry pending close"
            reason_code = "RETRY_CLOSE"
        else:
            reason_label = "Global cap eviction (oldest)"
            reason_code = "GLOBAL_CAP_EVICTION"

        msg = (
            f"⚠️ {reason_label}: Ticket {ticket} "
            f"({symbol}, profit={profit})"
        )

        if executor.mode == "live":
            now = time.time()
            if not isinstance(getattr(executor, "_last_close_log", None), dict):
                executor._last_close_log = {}
            last_log = executor._last_close_log.get(ticket, 0.0)
            should_log = (now - last_log >= 10.0) or (decision != "close" and ticket not in policy.pending_closes)

            if should_log:
                logger.warning(msg)
                executor._last_close_log[ticket] = now

            success = executor.close_ticket(t, reason=reason_label)
            policy.report_close_result(ticket, t, success)
            if success:
                executor._last_close_log.pop(ticket, None)
                store.log_action(reason_code, t)
                notifier.send(msg)
                closed_count += 1
            else:
                if should_log:
                    logger.error("Failed to close ticket %d for %s.", ticket, reason_label)
        else:
            # Observe mode: simulate logging without permanently marking as closed
            # on the broker. Keep pending so switching to live mode executes it.
            if ticket not in policy.pending_closes:
                logger.warning("[OBSERVE] %s", msg)
                executor.close_ticket(t, reason=reason_label)
                notifier.send(f"[OBSERVE] {msg}")
                closed_count += 1
            policy.report_close_result(ticket, t, success=False)

    return closed_count


def run_iteration(
    config, executor, store, notifier, policy, mt5_online, cooldown_until,
    prev_pos_count,
):
    """Run one guardian loop iteration.

    Returns ``(mt5_online, cooldown_until, current_pos_count)``.
    """
    if not reload_config(config):
        return mt5_online, cooldown_until, prev_pos_count

    loop_interval, reset_hour, server_time_symbol, cooldown_sec, account_changed = apply_runtime_config(
        config, executor, notifier
    )

    if account_changed:
        logger.info("Account credentials changed in config; reconnecting and resetting policy.")
        policy.reset()
        prev_pos_count = 0
        if not executor.reconnect():
            notifier.send("🚨 MT5 Guardian failed to reconnect after account config change.")
            time.sleep(1)
            return mt5_online, cooldown_until, prev_pos_count

    account, mt5_online = ensure_mt5_connection(executor, notifier, mt5_online)
    if not account:
        time.sleep(1)
        return mt5_online, cooldown_until, prev_pos_count

    now = time.time()
    if cooldown_until and now < cooldown_until:
        time.sleep(loop_interval)
        return mt5_online, cooldown_until, prev_pos_count

    # Validate hedging account mode on first successful connection.
    if not executor.validate_hedging(account):
        if executor.mode == "live":
            if getattr(executor, "_non_hedging_alerted", False) is not True:
                logger.error("Live enforcement blocked: non-hedging account mode.")
                notifier.send("🚨 Live enforcement BLOCKED: non-hedging account mode!")
                executor._non_hedging_alerted = True
            time.sleep(loop_interval)
            return mt5_online, cooldown_until, prev_pos_count

    equity = float(account.get("equity", 0.0))
    balance = float(account.get("balance", 0.0))

    if balance <= 0 or equity <= 0:
        logger.warning("Account balance or equity is 0. MT5 might still be syncing. Skipping loop.")
        time.sleep(1)
        return mt5_online, cooldown_until, prev_pos_count

    positions = executor.get_positions()
    pos_count = len(positions)

    # In observe mode, track effective active positions (excluding already pending policy
    # closes and already observed unprotected closes) so repeat iterations with static
    # pending closes do not repeatedly trigger peak resets.
    if executor.mode != "live":
        already_simulated = set(policy.pending_closes.keys()) | getattr(executor, "_observed_unprotected", set())
        if already_simulated:
            active_managed_count = len([p for p in positions if int(p["ticket"]) not in already_simulated])
            max_pos_count_in_cycle = max(prev_pos_count, active_managed_count)
            pos_count = active_managed_count
        else:
            max_pos_count_in_cycle = max(prev_pos_count, pos_count)
    else:
        max_pos_count_in_cycle = max(prev_pos_count, pos_count)

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
        return mt5_online, cooldown_until, pos_count

    # ---- Position policy: same-symbol admission → global cap → retries ---
    tickets_to_close = policy.evaluate(positions, config)
    closed_count = 0
    if tickets_to_close:
        closed_count = _close_policy_tickets(
            executor, store, notifier, policy, tickets_to_close
        )

    if closed_count > 0:
        if executor.mode == "live":
            positions = executor.get_positions()
            pos_count = len(positions)
            refreshed_account = executor.get_account()
            if refreshed_account:
                equity = float(refreshed_account.get("equity", equity))
                balance = float(refreshed_account.get("balance", balance))
        else:
            pos_count = max(0, pos_count - closed_count)

    # ---- SL enforcement: single unified pass for stop-losses & unprotected closes ----
    unprotected = executor.manage_stop_losses(positions, balance)
    if unprotected:
        for pos in unprotected:
            ticket = int(pos["ticket"])
            symbol = pos.get("symbol", "")
            profit = pos.get("profit", 0.0)
            vol = pos.get("volume", 0.0)
            if executor.mode == "live":
                store.log_action("UNPROTECTED_CLOSE", pos)
                notifier.send(
                    f"🛡️ Closed unprotected ticket {ticket} ({symbol}, {vol} lots, profit={profit})."
                )
            else:
                store.log_action("UNPROTECTED_CLOSE", pos)
                notifier.send(
                    f"[OBSERVE] Would close unprotected ticket {ticket} ({symbol}, {vol} lots, profit={profit})."
                )

        if executor.mode == "live":
            positions = executor.get_positions()
            pos_count = len(positions)
            refreshed_account = executor.get_account()
            if refreshed_account:
                equity = float(refreshed_account.get("equity", equity))
                balance = float(refreshed_account.get("balance", balance))
        else:
            pos_count = max(0, pos_count - len(unprotected))

    # ---- 2→1 peak reset: when count drops from >= 2 to 1 ------------------
    # Covers: multi-iteration drop, same-loop 1→2→1 replacement, and manage_stop_losses
    if max_pos_count_in_cycle >= 2 and pos_count == 1:
        logger.info(
            "Position count fell from %d to %d. "
            "Resetting peak equity from %.2f to current equity %.2f.",
            max_pos_count_in_cycle,
            pos_count,
            peak_equity,
            equity,
        )
        peak_equity = equity
        store.save_state(day_key, start_balance, peak_equity, breaker_tripped)
        notifier.send(
            f"📊 Peak equity reset to {equity:.2f} "
            f"(positions {max_pos_count_in_cycle} → {pos_count})."
        )

    # ---- Trailing equity stop --------------------------------------------
    threshold = trailing_threshold(peak_equity, balance, pos_count, config)
    if pos_count > 0 and equity <= threshold:
        msg = f"💥 TRAILING STOP HIT! Equity {equity:.2f} <= {threshold:.2f} (Peak: {peak_equity:.2f})"
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
    return mt5_online, cooldown_until, pos_count


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
    policy = PositionPolicy()

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
    prev_pos_count = 0

    try:
        while True:
            try:
                mt5_online, cooldown_until, prev_pos_count = run_iteration(
                    config, executor, store, notifier, policy,
                    mt5_online, cooldown_until, prev_pos_count,
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
                        "Heartbeat: account=%s equity=%s balance=%s mode=%s positions=%d",
                        account.get("login"),
                        account.get("equity"),
                        account.get("balance"),
                        executor.mode,
                        prev_pos_count,
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
