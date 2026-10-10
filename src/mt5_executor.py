import logging
import time

import MetaTrader5 as mt5

from risk_engine import per_position_risk_amount

logger = logging.getLogger(__name__)

VALID_MODES = frozenset({"observe", "live"})

# MQL5 bitmask values; not always exported by the Python MetaTrader5 package.
SYMBOL_FILLING_FOK = getattr(mt5, "SYMBOL_FILLING_FOK", 1)
SYMBOL_FILLING_IOC = getattr(mt5, "SYMBOL_FILLING_IOC", 2)
SYMBOL_FILLING_RETURN = getattr(mt5, "SYMBOL_FILLING_RETURN", 4)

# MT5 account margin modes.
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = getattr(
    mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2
)


def normalize_mode(mode):
    """Only explicit 'live' executes trades; everything else is observe."""
    normalized = str(mode or "observe").strip().lower()
    if normalized not in VALID_MODES:
        logger.warning("Unknown mode '%s'; defaulting to observe.", mode)
        return "observe"
    return normalized


def resolve_filling_modes(symbol_info):
    """Return broker-supported filling modes in preferred order."""
    filling_mode = getattr(symbol_info, "filling_mode", 0)
    candidates = []

    if filling_mode & SYMBOL_FILLING_IOC:
        candidates.append(mt5.ORDER_FILLING_IOC)
    if filling_mode & SYMBOL_FILLING_FOK:
        candidates.append(mt5.ORDER_FILLING_FOK)
    if filling_mode & SYMBOL_FILLING_RETURN:
        candidates.append(mt5.ORDER_FILLING_RETURN)

    if not candidates:
        candidates = [
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN,
        ]

    return candidates


class MT5Executor:
    def __init__(self, config):
        self.config = config
        self.connected = False
        self._account_key = None
        self._hedging_validated = False
        self._hedging_ok = True
        self._non_hedging_alerted = False
        self._sl_failures: dict[int, int] = {}
        self._observed_unprotected: set[int] = set()
        self._last_unprotected_log: dict[int, float] = {}
        self._last_close_fail_log: dict[int, float] = {}
        self._last_emergency_close_attempt: dict[int, float] = {}
        self.apply_config(config)

    def _read_execution(self, config):
        execution = config.get("execution", {})
        return {
            "close_deviation": int(execution.get("close_deviation", 50)),
            "close_retries": int(execution.get("close_retries", 3)),
            "emergency_close_retry_interval": float(execution.get("emergency_close_retry_interval", 5.0)),
        }

    def _read_filters(self, config):
        filters = config.get("filters", {})
        symbols = filters.get("symbols", [])
        if isinstance(symbols, str):
            symbols = [symbols]
        symbol_whitelist = {s.strip() for s in symbols if s and str(s).strip()}
        magic_numbers = filters.get("magic_numbers", [])
        if isinstance(magic_numbers, (int, float)):
            magic_numbers = [int(magic_numbers)]
        magic_whitelist = {int(m) for m in magic_numbers} if magic_numbers else set()
        return symbol_whitelist, magic_whitelist

    def _account_credentials(self, config):
        acc_cfg = config.get("account", {})
        return (
            int(acc_cfg.get("login", 0) or 0),
            acc_cfg.get("password", "") or "",
            acc_cfg.get("server", "") or "",
        )

    def apply_config(self, config):
        """Apply config values. Returns True if account credentials changed (reconnect required)."""
        old_account_key = self._account_key
        self.config = config
        self.mode = normalize_mode(config.get("mode", "observe"))
        execution = self._read_execution(config)
        self.close_deviation = execution["close_deviation"]
        self.close_retries = execution["close_retries"]
        self.emergency_close_retry_interval = execution["emergency_close_retry_interval"]
        self.symbol_whitelist, self.magic_whitelist = self._read_filters(config)
        self._account_key = self._account_credentials(config)
        account_changed = old_account_key is not None and self._account_key != old_account_key
        if account_changed or self.mode == "live":
            self._observed_unprotected.clear()
        if account_changed:
            self._hedging_validated = False
            self._hedging_ok = True
            self._non_hedging_alerted = False
            self._sl_failures.clear()
            self._last_unprotected_log.clear()
            self._last_close_fail_log.clear()
            self._last_emergency_close_attempt.clear()
        return account_changed

    def position_managed(self, position):
        """Return True if this position is within configured symbol/magic filters."""
        if self.symbol_whitelist and position.get("symbol") not in self.symbol_whitelist:
            return False
        if self.magic_whitelist and int(position.get("magic", 0)) not in self.magic_whitelist:
            return False
        return True

    def connect(self):
        if not mt5.initialize():
            logger.error("initialize() failed, error code: %s", mt5.last_error())
            self.connected = False
            return False

        login, password, server = self._account_key or self._account_credentials(self.config)

        if login:
            authorized = mt5.login(login=login, password=password, server=server)
            if not authorized:
                logger.error(
                    "Failed to connect to account #%s, error code: %s",
                    login,
                    mt5.last_error(),
                )
                self.connected = False
                return False
            logger.info("Logged into MT5 account #%s on server %s", login, server)
        else:
            logger.info("Connected to currently active MT5 account. Version: %s", mt5.version())

        self.connected = True
        self._hedging_validated = False
        self._non_hedging_alerted = False
        return True

    def reconnect(self):
        logger.warning("Attempting MT5 reconnect...")
        mt5.shutdown()
        time.sleep(0.5)
        self._sl_failures.clear()
        self._observed_unprotected.clear()
        self._last_unprotected_log.clear()
        self._last_close_fail_log.clear()
        self._last_emergency_close_attempt.clear()
        self._non_hedging_alerted = False
        return self.connect()

    def get_account(self):
        if not self.connected:
            return None
        acc = mt5.account_info()
        if acc is None:
            logger.error("Failed to get account info, error code: %s", mt5.last_error())
            self.connected = False
            return None
        return acc._asdict()

    def get_positions(self):
        positions = mt5.positions_get()
        if positions is None:
            return []
        managed = [p._asdict() for p in positions if self.position_managed(p._asdict())]
        return managed

    def _ensure_symbol_selected(self, symbol):
        if not mt5.symbol_select(symbol, True):
            logger.error("Failed to select symbol %s in Market Watch.", symbol)
            return False
        return True

    # ------------------------------------------------------------------
    # Hedging account validation
    # ------------------------------------------------------------------

    def validate_hedging(self, account_dict):
        """Check that the account is in hedging mode.

        On non-hedging accounts, separate same-symbol tickets cannot be
        managed independently.  In observe mode this logs a warning; in
        live mode enforcement is blocked and the method returns ``False``.
        """
        if self._hedging_validated:
            return self._hedging_ok

        margin_mode = int(account_dict.get("margin_mode", 0))
        if margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            self._hedging_ok = True
        else:
            self._hedging_ok = False
            if self.mode == "live":
                logger.error(
                    "Account margin mode %d is NOT hedging (expected %d). "
                    "Two-position management requires hedging. "
                    "Live enforcement is BLOCKED.",
                    margin_mode,
                    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
                )
            else:
                logger.warning(
                    "[OBSERVE] Account margin mode %d is NOT hedging. "
                    "Two-position management would not work on this account.",
                    margin_mode,
                )
        self._hedging_validated = True
        return self._hedging_ok

    # ------------------------------------------------------------------
    # Per-ticket independent stop-loss
    # ------------------------------------------------------------------

    def ensure_stop_losses(self, positions, account_balance):
        """Set independent 5% SL per ticket.

        Each ticket gets its own ``balance * risk_pct_per_position / 100``
        risk budget.  No volume-split, no 1.10 multiplier.
        """
        if not positions:
            return 0

        risk_amount = per_position_risk_amount(account_balance, self.config)
        updated = 0
        unprotected: list[int] = []
        for position in positions:
            result = self.ensure_stop_loss(position, risk_amount)
            if result is True:
                updated += 1
            elif result == "unprotected":
                unprotected.append(int(position["ticket"]))

        if unprotected:
            logger.warning(
                "Unprotected tickets (SL could not be set within budget): %s",
                unprotected,
            )
        return updated

    def ensure_stop_loss(self, position, failsafe_loss_amount):
        """Set or tighten a single ticket's broker SL.

        Rules:
        - Never widen an existing tighter SL.
        - Set missing SLs.
        - Tighten looser SLs.
        - Validate against current bid/ask and broker stop level.
        - If SL cannot be placed within the 5% budget, report unprotected.

        Returns ``True`` if SL is adequate, ``False`` on error, or the
        string ``"unprotected"`` when the ticket cannot be protected within
        the risk budget.
        """
        symbol = position["symbol"]
        if not self._ensure_symbol_selected(symbol):
            return False

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return False

        tick_value = getattr(symbol_info, "trade_tick_value", None)
        tick_size = getattr(symbol_info, "trade_tick_size", None)
        point = getattr(symbol_info, "point", None)
        try:
            volume = float(position.get("volume", 0.0) or 0.0)
        except (ValueError, TypeError):
            volume = 0.0

        if (
            tick_size is None
            or tick_value is None
            or point is None
            or tick_size <= 0
            or tick_value <= 0
            or point <= 0
            or volume <= 0
            or failsafe_loss_amount <= 0
        ):
            now = time.time()
            last_logged = self._last_unprotected_log.get(position["ticket"], 0.0)
            if now - last_logged >= 10.0:
                logger.warning(
                    "Ticket %d (%s): invalid tick specs (tick_size=%s, tick_value=%s, point=%s, volume=%s, risk=%s). "
                    "Cannot guarantee 5%% risk stop; marking unprotected.",
                    position["ticket"],
                    symbol,
                    tick_size,
                    tick_value,
                    point,
                    volume,
                    failsafe_loss_amount,
                )
                self._last_unprotected_log[position["ticket"]] = now
            return "unprotected"

        loss_per_point = volume * (point / tick_size) * tick_value
        if loss_per_point <= 0:
            return "unprotected"

        sl_distance = failsafe_loss_amount / loss_per_point * point

        if position["type"] == mt5.ORDER_TYPE_BUY:
            sl_price = position["price_open"] - sl_distance
        elif position["type"] == mt5.ORDER_TYPE_SELL:
            sl_price = position["price_open"] + sl_distance
        else:
            return False

        sl_price = round(sl_price, symbol_info.digits)

        # Preserve tighter manual/existing SLs — never widen protection.
        current_sl = float(position.get("sl", 0.0) or 0.0)
        if current_sl != 0.0:
            if position["type"] == mt5.ORDER_TYPE_BUY:
                # For buys, a higher SL is tighter.
                if current_sl > sl_price:
                    # Existing SL is tighter — keep it.
                    return True
                # Existing SL equals ours (within a point) — nothing to do.
                if abs(current_sl - sl_price) < point:
                    return True
            elif position["type"] == mt5.ORDER_TYPE_SELL:
                # For sells, a lower SL is tighter.
                if current_sl < sl_price:
                    return True
                if abs(current_sl - sl_price) < point:
                    return True

        # Validate against current bid/ask and broker minimum stop level.
        # In MT5, stops_level is the minimum distance from current market price
        # (Bid for BUY SL, Ask for SELL SL), NOT from price_open.
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or getattr(tick, "bid", 0) <= 0 or getattr(tick, "ask", 0) <= 0:
            logger.error("Failed to get tick for %s", symbol)
            return False

        min_stop_distance = float(getattr(symbol_info, "trade_stops_level", 0)) * point

        now = time.time()
        if position["type"] == mt5.ORDER_TYPE_BUY:
            max_allowed_sl = round(tick.bid - min_stop_distance, symbol_info.digits)
            if sl_price > max_allowed_sl:
                last_logged = self._last_unprotected_log.get(position["ticket"], 0.0)
                if now - last_logged >= 10.0:
                    logger.warning(
                        "Ticket %d (BUY): computed SL %.5f > max allowed %.5f (bid=%.5f, stops_level=%.5f). "
                        "Cannot protect within 5%% budget.",
                        position["ticket"],
                        sl_price,
                        max_allowed_sl,
                        tick.bid,
                        min_stop_distance,
                    )
                    self._last_unprotected_log[position["ticket"]] = now
                return "unprotected"
        elif position["type"] == mt5.ORDER_TYPE_SELL:
            min_allowed_sl = round(tick.ask + min_stop_distance, symbol_info.digits)
            if sl_price < min_allowed_sl:
                last_logged = self._last_unprotected_log.get(position["ticket"], 0.0)
                if now - last_logged >= 10.0:
                    logger.warning(
                        "Ticket %d (SELL): computed SL %.5f < min allowed %.5f (ask=%.5f, stops_level=%.5f). "
                        "Cannot protect within 5%% budget.",
                        position["ticket"],
                        sl_price,
                        min_allowed_sl,
                        tick.ask,
                        min_stop_distance,
                    )
                    self._last_unprotected_log[position["ticket"]] = now
                return "unprotected"

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "sl": float(sl_price),
            "tp": float(position["tp"]),
            "position": position["ticket"],
        }

        if self.mode != "live":
            logger.info(
                "[OBSERVE] Would ensure failsafe SL=%s for ticket %s",
                sl_price,
                position["ticket"],
            )
            return True

        result = mt5.order_send(request)
        if result is None:
            logger.error(
                "Failed to set SL for %s: order_send returned None (%s)",
                position["ticket"],
                mt5.last_error(),
            )
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "Failed to set SL for %s: %s (Retcode: %s)",
                position["ticket"],
                result.comment,
                result.retcode,
            )
            return False
        logger.info("Set broker failsafe SL=%s for ticket %s", sl_price, position["ticket"])
        return True

    def manage_stop_losses(self, positions, account_balance):
        """Ensure Stop Loss protection on all open positions in a single pass.

        - Preserves tighter manual/existing SLs.
        - Sets or tightens SLs within the 5% per-ticket risk budget.
        - Closes any ticket that cannot be protected within budget (stops_level breach).
        - Closes any naked ticket where SL placement persistently fails (failed order_send).

        Returns a list of positions that were closed as unprotected.
        """
        if not positions:
            self._sl_failures.clear()
            self._observed_unprotected.clear()
            self._last_unprotected_log.clear()
            self._last_close_fail_log.clear()
            self._last_emergency_close_attempt.clear()
            return []

        risk_amount = per_position_risk_amount(account_balance, self.config)
        closed_unprotected = []
        current_tickets = {int(p["ticket"]) for p in positions}

        # Clear failure counts and tracking for tickets that are no longer open
        for t in list(self._sl_failures):
            if t not in current_tickets:
                del self._sl_failures[t]
        for t in list(self._observed_unprotected):
            if t not in current_tickets:
                self._observed_unprotected.remove(t)
        for t in list(self._last_unprotected_log):
            if t not in current_tickets:
                del self._last_unprotected_log[t]
        for t in list(self._last_close_fail_log):
            if t not in current_tickets:
                del self._last_close_fail_log[t]
        for t in list(self._last_emergency_close_attempt):
            if t not in current_tickets:
                del self._last_emergency_close_attempt[t]

        now = time.time()
        for position in positions:
            ticket = int(position["ticket"])
            result = self.ensure_stop_loss(position, risk_amount)

            if result is True:
                self._sl_failures.pop(ticket, None)
                self._observed_unprotected.discard(ticket)
            elif result == "unprotected":
                reason = "Unprotected (exceeds budget)"
                if self.mode == "live":
                    last_attempt = self._last_emergency_close_attempt.get(ticket, 0.0)
                    if now - last_attempt >= self.emergency_close_retry_interval:
                        self._last_emergency_close_attempt[ticket] = now
                        if self.close_ticket(position, reason=reason):
                            self._last_emergency_close_attempt.pop(ticket, None)
                            self._last_close_fail_log.pop(ticket, None)
                            logger.warning("Closed unprotected ticket %d (SL beyond budget).", ticket)
                            closed_unprotected.append(position)
                        else:
                            last_fail = self._last_close_fail_log.get(ticket, 0.0)
                            if now - last_fail >= 10.0:
                                logger.error("Failed to close unprotected ticket %d.", ticket)
                                self._last_close_fail_log[ticket] = now
                else:
                    if ticket not in self._observed_unprotected:
                        logger.warning("[OBSERVE] Would close unprotected ticket %d.", ticket)
                        self._observed_unprotected.add(ticket)
                        closed_unprotected.append(position)
            elif result is False:
                # order_send or tick lookup failed
                self._sl_failures[ticket] = self._sl_failures.get(ticket, 0) + 1
                last_fail = self._last_close_fail_log.get(ticket, 0.0)
                if now - last_fail >= 10.0:
                    logger.warning(
                        "Ticket %d: SL placement failed (attempt %d/%d).",
                        ticket, self._sl_failures[ticket], self.close_retries
                    )
                    self._last_close_fail_log[ticket] = now

                if self._sl_failures[ticket] >= self.close_retries:
                    reason = "Unprotected (persistent SL)"
                    if self.mode == "live":
                        last_attempt = self._last_emergency_close_attempt.get(ticket, 0.0)
                        if now - last_attempt >= self.emergency_close_retry_interval:
                            self._last_emergency_close_attempt[ticket] = now
                            last_err = self._last_unprotected_log.get(ticket, 0.0)
                            if now - last_err >= 10.0:
                                logger.error(
                                    "Ticket %d: persistent SL placement failure. Closing naked position!",
                                    ticket
                                )
                                self._last_unprotected_log[ticket] = now
                            if self.close_ticket(position, reason=reason):
                                self._last_emergency_close_attempt.pop(ticket, None)
                                self._last_close_fail_log.pop(ticket, None)
                                logger.warning("Closed naked ticket %d after persistent SL failures.", ticket)
                                closed_unprotected.append(position)
                            else:
                                last_cfail = self._last_close_fail_log.get(ticket, 0.0)
                                if now - last_cfail >= 10.0:
                                    logger.error("Failed to close naked ticket %d.", ticket)
                                    self._last_close_fail_log[ticket] = now
                    else:
                        if ticket not in self._observed_unprotected:
                            logger.warning("[OBSERVE] Would close naked ticket %d after persistent SL failures.", ticket)
                            self._observed_unprotected.add(ticket)
                            closed_unprotected.append(position)

        return closed_unprotected

    def close_unprotected(self, positions, account_balance):
        """Close tickets that cannot be SL-protected within the risk budget."""
        return self.manage_stop_losses(positions, account_balance)

    def close_ticket(self, position, retries=None, reason="Guardian Close"):
        symbol = position["symbol"]
        ticket = position["ticket"]
        lot = float(position["volume"])
        pos_type = position["type"]
        retries = self.close_retries if retries is None else retries

        if not self._ensure_symbol_selected(symbol):
            return False

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return False

        if pos_type == mt5.ORDER_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
        elif pos_type == mt5.ORDER_TYPE_SELL:
            order_type = mt5.ORDER_TYPE_BUY
        else:
            return False

        base_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "position": ticket,
            "deviation": self.close_deviation,
            "magic": int(position.get("magic", 0)),
            "comment": str(reason)[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        if self.mode != "live":
            logger.warning(
                "[OBSERVE] Would close ticket %s (%s lots %s, profit: %s) reason: %s",
                ticket,
                lot,
                symbol,
                position.get("profit"),
                reason,
            )
            return True

        filling_modes = resolve_filling_modes(symbol_info)

        for attempt in range(retries):
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue

            price = tick.bid if pos_type == mt5.ORDER_TYPE_BUY else tick.ask

            for filling_mode in filling_modes:
                request = dict(base_request)
                request["price"] = price
                request["type_filling"] = filling_mode

                result = mt5.order_send(request)
                if result is None:
                    logger.error(
                        "Close attempt %s failed for %s: order_send returned None (%s)",
                        attempt + 1,
                        ticket,
                        mt5.last_error(),
                    )
                    continue
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    logger.info("Successfully closed ticket %s", ticket)
                    return True
                if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                    continue
                logger.error(
                    "Close attempt %s failed for %s: %s (Retcode: %s)",
                    attempt + 1,
                    ticket,
                    result.comment,
                    result.retcode,
                )
                break

            time.sleep(0.1)

        return False

    def close_all(self, reason=""):
        positions = self.get_positions()
        if not positions:
            return 0

        logger.warning("CLOSING ALL POSITIONS (%s). Reason: %s", len(positions), reason)

        positions_sorted = sorted(positions, key=lambda x: (x["volume"], -x["profit"]), reverse=True)

        closed_count = 0
        for pos in positions_sorted:
            if self.close_ticket(pos, reason=reason):
                closed_count += 1

        return closed_count
