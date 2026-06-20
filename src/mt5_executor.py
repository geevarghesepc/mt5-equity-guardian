import logging
import time

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

VALID_MODES = frozenset({"observe", "live"})

# MQL5 bitmask values; not always exported by the Python MetaTrader5 package.
SYMBOL_FILLING_FOK = getattr(mt5, "SYMBOL_FILLING_FOK", 1)
SYMBOL_FILLING_IOC = getattr(mt5, "SYMBOL_FILLING_IOC", 2)
SYMBOL_FILLING_RETURN = getattr(mt5, "SYMBOL_FILLING_RETURN", 4)


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
        self.apply_config(config)

    def _read_execution(self, config):
        execution = config.get("execution", {})
        return {
            "close_deviation": int(execution.get("close_deviation", 50)),
            "close_retries": int(execution.get("close_retries", 3)),
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
        self.broker_sl_points = config.get("failsafe", {}).get("broker_sl_points", 500)
        execution = self._read_execution(config)
        self.close_deviation = execution["close_deviation"]
        self.close_retries = execution["close_retries"]
        self.symbol_whitelist, self.magic_whitelist = self._read_filters(config)
        self._account_key = self._account_credentials(config)
        return old_account_key is not None and self._account_key != old_account_key

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
        return True

    def reconnect(self):
        logger.warning("Attempting MT5 reconnect...")
        mt5.shutdown()
        time.sleep(0.5)
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

    def ensure_stop_loss(self, position, current_equity, pos_count):
        if position["sl"] != 0.0:
            return

        symbol = position["symbol"]
        if not self._ensure_symbol_selected(symbol):
            return

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return

        thresholds = self.config.get("thresholds", {})
        risk_pct = (
            float(thresholds.get("averaging_risk_pct", 15.0))
            if pos_count > 1
            else float(thresholds.get("base_risk_pct", 10.0))
        )

        target_loss_amount = (current_equity * (risk_pct / 100.0)) / max(1, pos_count)
        failsafe_loss_amount = target_loss_amount * 1.10

        tick_value = symbol_info.trade_tick_value
        tick_size = symbol_info.trade_tick_size
        point = symbol_info.point
        volume = float(position["volume"])

        if tick_size > 0 and tick_value > 0 and volume > 0:
            loss_per_point = volume * (point / tick_size) * tick_value
            sl_points_adjusted = failsafe_loss_amount / loss_per_point * point
        else:
            sl_points_adjusted = self.broker_sl_points * point

        min_stop_distance = symbol_info.trade_stops_level * point
        if min_stop_distance > 0 and sl_points_adjusted < min_stop_distance:
            sl_points_adjusted = min_stop_distance

        if position["type"] == mt5.ORDER_TYPE_BUY:
            sl_price = position["price_open"] - sl_points_adjusted
            sl_price = round(sl_price, symbol_info.digits)
        elif position["type"] == mt5.ORDER_TYPE_SELL:
            sl_price = position["price_open"] + sl_points_adjusted
            sl_price = round(sl_price, symbol_info.digits)
        else:
            return

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

    def close_ticket(self, position, retries=None):
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
            "comment": "Guardian Close",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        if self.mode != "live":
            logger.warning(
                "[OBSERVE] Would close ticket %s (%s lots %s, profit: %s)",
                ticket,
                lot,
                symbol,
                position["profit"],
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
            if self.close_ticket(pos):
                closed_count += 1

        return closed_count
