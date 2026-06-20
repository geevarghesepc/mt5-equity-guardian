import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import MetaTrader5 as mt5
from mt5_executor import (
    MT5Executor,
    normalize_mode,
    resolve_filling_modes,
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
)


def test_normalize_mode_defaults_unknown_to_observe():
    assert normalize_mode("LIVE") == "live"
    assert normalize_mode("observe") == "observe"
    assert normalize_mode("paper") == "observe"
    assert normalize_mode(None) == "observe"


def test_resolve_filling_modes_prefers_broker_flags():
    symbol_info = SimpleNamespace(filling_mode=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC)
    modes = resolve_filling_modes(symbol_info)
    assert modes[0] == mt5.ORDER_FILLING_IOC
    assert mt5.ORDER_FILLING_FOK in modes


def test_position_managed_respects_filters():
    executor = MT5Executor(
        {
            "mode": "observe",
            "filters": {"symbols": ["EURUSD"], "magic_numbers": [42]},
        }
    )
    assert executor.position_managed({"symbol": "EURUSD", "magic": 42})
    assert not executor.position_managed({"symbol": "GBPUSD", "magic": 42})
    assert not executor.position_managed({"symbol": "EURUSD", "magic": 7})


@patch("mt5_executor.mt5")
def test_ensure_stop_loss_respects_stops_level(mock_mt5):
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.TRADE_ACTION_SLTP = mt5.TRADE_ACTION_SLTP
    mock_mt5.TRADE_RETCODE_DONE = mt5.TRADE_RETCODE_DONE
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=500,
    )
    mock_mt5.order_send.return_value = SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, comment="ok")

    executor = MT5Executor({"mode": "live", "thresholds": {"base_risk_pct": 10.0}})
    position = {
        "sl": 0.0,
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.01,
        "tp": 0.0,
        "ticket": 100,
    }

    assert executor.ensure_stop_loss(position, current_equity=10000, pos_count=1) is True
    request = mock_mt5.order_send.call_args[0][0]
    distance = position["price_open"] - request["sl"]
    assert distance >= 500 * 0.00001


@patch("mt5_executor.mt5")
def test_close_ticket_handles_none_order_send(mock_mt5):
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.ORDER_TYPE_SELL = mt5.ORDER_TYPE_SELL
    mock_mt5.TRADE_ACTION_DEAL = mt5.TRADE_ACTION_DEAL
    mock_mt5.ORDER_TIME_GTC = mt5.ORDER_TIME_GTC
    mock_mt5.last_error.return_value = (1, "error")
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(filling_mode=SYMBOL_FILLING_IOC)
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.1, ask=1.1002)
    mock_mt5.order_send.return_value = None

    executor = MT5Executor({"mode": "live", "execution": {"close_retries": 1}})
    position = {
        "symbol": "EURUSD",
        "ticket": 1,
        "volume": 0.1,
        "type": mt5.ORDER_TYPE_BUY,
        "profit": -5.0,
        "magic": 0,
    }

    assert executor.close_ticket(position) is False
