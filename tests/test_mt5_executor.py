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
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
)


# ---------------------------------------------------------------------------
# normalize_mode
# ---------------------------------------------------------------------------

def test_normalize_mode_defaults_unknown_to_observe():
    assert normalize_mode("LIVE") == "live"
    assert normalize_mode("observe") == "observe"
    assert normalize_mode("paper") == "observe"
    assert normalize_mode(None) == "observe"


# ---------------------------------------------------------------------------
# Filling modes
# ---------------------------------------------------------------------------

def test_resolve_filling_modes_prefers_broker_flags():
    symbol_info = SimpleNamespace(filling_mode=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC)
    modes = resolve_filling_modes(symbol_info)
    assert modes[0] == mt5.ORDER_FILLING_IOC
    assert mt5.ORDER_FILLING_FOK in modes


# ---------------------------------------------------------------------------
# Position filtering
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Hedging validation
# ---------------------------------------------------------------------------

def test_validate_hedging_pass():
    executor = MT5Executor({"mode": "live"})
    result = executor.validate_hedging(
        {"margin_mode": ACCOUNT_MARGIN_MODE_RETAIL_HEDGING}
    )
    assert result is True


def test_validate_hedging_fail_live():
    executor = MT5Executor({"mode": "live"})
    result = executor.validate_hedging({"margin_mode": 0})
    assert result is False


def test_validate_hedging_warn_observe():
    executor = MT5Executor({"mode": "observe"})
    result = executor.validate_hedging({"margin_mode": 0})
    assert result is False


def test_validate_hedging_cached():
    executor = MT5Executor({"mode": "live"})
    executor.validate_hedging({"margin_mode": ACCOUNT_MARGIN_MODE_RETAIL_HEDGING})
    # Second call should use cached result even with different data.
    result = executor.validate_hedging({"margin_mode": 0})
    assert result is True  # Still cached as True


# ---------------------------------------------------------------------------
# SL preservation — never widen
# ---------------------------------------------------------------------------

@patch("mt5_executor.mt5")
def test_ensure_stop_loss_preserves_tighter_sl(mock_mt5):
    """A tighter existing SL should not be replaced."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.TRADE_ACTION_SLTP = mt5.TRADE_ACTION_SLTP
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=0,
    )

    executor = MT5Executor({"mode": "live", "thresholds": {"risk_pct_per_position": 5.0}})
    position = {
        "sl": 1.09800,  # Already tighter (closer to price) than our computed SL.
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.01,
        "tp": 0.0,
        "ticket": 100,
    }

    # Failsafe of 50 → SL distance = 50 / (0.01 * 1.0) * 0.00001 = 0.05
    # So computed SL = 1.10000 - 0.05 = 1.05000
    # Existing SL 1.09800 is tighter (higher for a buy) → keep it.
    result = executor.ensure_stop_loss(position, failsafe_loss_amount=50.0)
    assert result is True
    mock_mt5.order_send.assert_not_called()


@patch("mt5_executor.mt5")
def test_ensure_stop_loss_tightens_loose_sl(mock_mt5):
    """A looser existing SL should be tightened."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.TRADE_ACTION_SLTP = mt5.TRADE_ACTION_SLTP
    mock_mt5.TRADE_RETCODE_DONE = mt5.TRADE_RETCODE_DONE
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=0,
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.09900, ask=1.09910)
    mock_mt5.order_send.return_value = SimpleNamespace(
        retcode=mt5.TRADE_RETCODE_DONE, comment="ok"
    )

    executor = MT5Executor({"mode": "live", "thresholds": {"risk_pct_per_position": 5.0}})
    position = {
        "sl": 1.00000,  # Very loose SL.
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.1,
        "tp": 0.0,
        "ticket": 100,
    }

    # Per-ticket budget of 500 on 0.1 lot → SL at 1.10000 - 0.005 = 1.09500
    # Existing 1.00000 is below → tighten to 1.09500.
    result = executor.ensure_stop_loss(position, failsafe_loss_amount=500.0)
    assert result is True
    assert mock_mt5.order_send.called
    request = mock_mt5.order_send.call_args[0][0]
    assert request["sl"] > 1.00000


# ---------------------------------------------------------------------------
# Broker stop distance exceeds budget → unprotected
# ---------------------------------------------------------------------------

@patch("mt5_executor.mt5")
def test_ensure_stop_loss_reports_unprotected(mock_mt5):
    """When broker min > SL budget → 'unprotected'."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=9999,  # Huge broker minimum.
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.10000, ask=1.10010)

    executor = MT5Executor({"mode": "live"})
    position = {
        "sl": 0.0,
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.01,
        "tp": 0.0,
        "ticket": 100,
    }

    result = executor.ensure_stop_loss(position, failsafe_loss_amount=10.0)
    assert result == "unprotected"


# ---------------------------------------------------------------------------
# Per-ticket independent SL (no volume split)
# ---------------------------------------------------------------------------

@patch("mt5_executor.mt5")
def test_ensure_stop_losses_independent_per_ticket(mock_mt5):
    """Each ticket gets the same dollar risk budget regardless of lot size."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.TRADE_ACTION_SLTP = mt5.TRADE_ACTION_SLTP
    mock_mt5.TRADE_RETCODE_DONE = mt5.TRADE_RETCODE_DONE
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=0,
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.09900, ask=1.09910)
    mock_mt5.order_send.return_value = SimpleNamespace(
        retcode=mt5.TRADE_RETCODE_DONE, comment="ok"
    )

    executor = MT5Executor(
        {"mode": "live", "thresholds": {"risk_pct_per_position": 5.0}}
    )
    positions = [
        {
            "sl": 0.0,
            "symbol": "EURUSD",
            "type": mt5.ORDER_TYPE_BUY,
            "price_open": 1.10000,
            "volume": 0.1,
            "tp": 0.0,
            "ticket": 1,
        },
        {
            "sl": 0.0,
            "symbol": "GBPUSD",
            "type": mt5.ORDER_TYPE_BUY,
            "price_open": 1.10000,
            "volume": 0.3,
            "tp": 0.0,
            "ticket": 2,
        },
    ]

    count = executor.ensure_stop_losses(positions, account_balance=10000)
    assert count == 2
    assert mock_mt5.order_send.call_count == 2

    # Both tickets get 5% of 10000 = 500 — not split by volume.
    # Ticket 1 (0.1 lot): SL distance = 500 / (0.1 * 1.0) * 0.00001 = 0.00500
    # Ticket 2 (0.3 lot): SL distance = 500 / (0.3 * 1.0) * 0.00001 = 0.00167
    calls = mock_mt5.order_send.call_args_list
    sl1 = calls[0][0][0]["sl"]
    sl2 = calls[1][0][0]["sl"]
    dist1 = 1.10000 - sl1
    dist2 = 1.10000 - sl2
    # Ticket with smaller volume gets a wider SL distance (same $ budget).
    assert dist1 > dist2


# ---------------------------------------------------------------------------
# close_ticket with reason
# ---------------------------------------------------------------------------

@patch("mt5_executor.mt5")
def test_close_ticket_uses_reason(mock_mt5):
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.ORDER_TYPE_SELL = mt5.ORDER_TYPE_SELL
    mock_mt5.TRADE_ACTION_DEAL = mt5.TRADE_ACTION_DEAL
    mock_mt5.ORDER_TIME_GTC = mt5.ORDER_TIME_GTC

    executor = MT5Executor({"mode": "observe"})
    executor.connected = True
    position = {
        "symbol": "EURUSD",
        "ticket": 1,
        "volume": 0.1,
        "type": mt5.ORDER_TYPE_BUY,
        "profit": -5.0,
        "magic": 0,
    }

    # In observe mode, close_ticket logs but doesn't send orders.
    result = executor.close_ticket(position, reason="Same-symbol loser")
    assert result is True


# ---------------------------------------------------------------------------
# close_ticket handles None order_send
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# manage_stop_losses: persistent SL failure closes naked position
# ---------------------------------------------------------------------------

@patch("mt5_executor.mt5")
def test_manage_stop_losses_persistent_failure_closes_naked_position(mock_mt5):
    """When broker order_send fails persistently (>= close_retries), ticket is closed."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.ORDER_TYPE_SELL = mt5.ORDER_TYPE_SELL
    mock_mt5.TRADE_ACTION_SLTP = mt5.TRADE_ACTION_SLTP
    mock_mt5.TRADE_ACTION_DEAL = mt5.TRADE_ACTION_DEAL
    mock_mt5.ORDER_TIME_GTC = mt5.ORDER_TIME_GTC
    mock_mt5.TRADE_RETCODE_DONE = 10009
    mock_mt5.last_error.return_value = (10015, "Invalid price")
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=0,
        filling_mode=SYMBOL_FILLING_IOC,
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.09900, ask=1.09910)
    # SL order fails:
    mock_mt5.order_send.return_value = SimpleNamespace(
        retcode=10015, comment="Invalid price"
    )

    executor = MT5Executor({"mode": "live", "execution": {"close_retries": 3}})
    position = {
        "sl": 0.0,
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.1,
        "tp": 0.0,
        "ticket": 42,
    }

    # Attempt 1: fails, not closed yet
    closed = executor.manage_stop_losses([position], account_balance=10000)
    assert closed == []
    assert executor._sl_failures[42] == 1

    # Attempt 2: fails, not closed yet
    closed = executor.manage_stop_losses([position], account_balance=10000)
    assert closed == []
    assert executor._sl_failures[42] == 2

    # Attempt 3: fails, reaches close_retries (3) -> triggers close_ticket!
    with patch.object(executor, "close_ticket", return_value=True) as mock_close:
        closed = executor.manage_stop_losses([position], account_balance=10000)
        assert len(closed) == 1
        assert closed[0]["ticket"] == 42
        mock_close.assert_called_once_with(
            position, reason="Unprotected (persistent SL)"
        )


@patch("mt5_executor.mt5")
def test_manage_stop_losses_success_clears_failure_count(mock_mt5):
    """A successful SL placement clears any previous failure count."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.TRADE_ACTION_SLTP = mt5.TRADE_ACTION_SLTP
    mock_mt5.TRADE_RETCODE_DONE = 10009
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=0,
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.09900, ask=1.09910)
    # Success
    mock_mt5.order_send.return_value = SimpleNamespace(
        retcode=10009, comment="Done"
    )

    executor = MT5Executor({"mode": "live"})
    executor._sl_failures[42] = 2  # had 2 previous failures

    position = {
        "sl": 0.0,
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.1,
        "tp": 0.0,
        "ticket": 42,
    }

    closed = executor.manage_stop_losses([position], account_balance=10000)
    assert closed == []
    assert 42 not in executor._sl_failures


@patch("mt5_executor.mt5")
def test_close_ticket_truncates_comment_to_31_chars(mock_mt5):
    """Emergency close reason strings longer than 31 chars must be truncated."""
    mock_mt5.ORDER_TYPE_BUY = 0
    mock_mt5.ORDER_TYPE_SELL = 1
    mock_mt5.TRADE_ACTION_DEAL = 1
    mock_mt5.ORDER_TIME_GTC = 0
    mock_mt5.TRADE_RETCODE_DONE = 10009
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(filling_mode=SYMBOL_FILLING_IOC)
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.1, ask=1.1002)
    mock_mt5.order_send.return_value = SimpleNamespace(retcode=10009, comment="Done")

    executor = MT5Executor({"mode": "live"})
    executor.connected = True
    position = {
        "symbol": "EURUSD",
        "ticket": 42,
        "volume": 0.1,
        "type": 0,
        "profit": -50.0,
        "magic": 0,
    }

    long_reason = "Unprotected (persistent SL order failure)"
    assert len(long_reason) > 31

    assert executor.close_ticket(position, reason=long_reason) is True
    call_request = mock_mt5.order_send.call_args[0][0]
    sent_comment = call_request["comment"]
    assert len(sent_comment) <= 31
    assert sent_comment == long_reason[:31]


@patch("mt5_executor.mt5")
def test_manage_stop_losses_observe_mode_does_not_repeat_unprotected_closures(mock_mt5):
    """In observe mode, an unprotected position is reported as simulated closed once."""
    mock_mt5.ORDER_TYPE_BUY = 0
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=9999,  # Unprotected
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.1, ask=1.1002)

    executor = MT5Executor({"mode": "observe"})
    position = {
        "sl": 0.0,
        "symbol": "EURUSD",
        "type": 0,
        "price_open": 1.10000,
        "volume": 0.1,
        "tp": 0.0,
        "ticket": 100,
    }

    # Loop 1: newly detected unprotected
    closed1 = executor.manage_stop_losses([position], account_balance=10000)
    assert len(closed1) == 1
    assert closed1[0]["ticket"] == 100
    assert 100 in executor._observed_unprotected

    # Loop 2: same position still open in observe mode -> returns empty, does not repeat
    closed2 = executor.manage_stop_losses([position], account_balance=10000)
    assert closed2 == []

    # Position finally closed/gone on MT5 -> cleaned up from _observed_unprotected
    executor.manage_stop_losses([], account_balance=10000)
    assert 100 not in executor._observed_unprotected


@patch("mt5_executor.mt5")
def test_ensure_stop_loss_invalid_specs_returns_unprotected(mock_mt5):
    """Invalid tick_value, tick_size, volume, or risk amount must return 'unprotected' instead of fallback."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=0.0,  # Invalid tick value!
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=0,
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.1, ask=1.1001)

    executor = MT5Executor({"mode": "live"})
    pos = {
        "sl": 0.0,
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.1,
        "tp": 0.0,
        "ticket": 501,
    }

    result = executor.ensure_stop_loss(pos, failsafe_loss_amount=500.0)
    assert result == "unprotected", "Invalid tick value must return unprotected"

    # Also test invalid volume
    mock_mt5.symbol_info.return_value.trade_tick_value = 1.0
    pos_invalid_vol = dict(pos, volume=0.0)
    assert executor.ensure_stop_loss(pos_invalid_vol, failsafe_loss_amount=500.0) == "unprotected"

    # Also test invalid risk amount
    assert executor.ensure_stop_loss(pos, failsafe_loss_amount=0.0) == "unprotected"


@patch("mt5_executor.mt5")
def test_manage_stop_losses_throttles_failed_emergency_closes(mock_mt5):
    """When an emergency close fails, subsequent loops within retry interval must not send market orders."""
    mock_mt5.ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info.return_value = SimpleNamespace(
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        point=0.00001,
        digits=5,
        trade_stops_level=9999,  # Unprotected trigger
    )
    mock_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.1, ask=1.1002)

    executor = MT5Executor({
        "mode": "live",
        "execution": {"emergency_close_retry_interval": 5.0},
    })
    position = {
        "sl": 0.0,
        "symbol": "EURUSD",
        "type": mt5.ORDER_TYPE_BUY,
        "price_open": 1.10000,
        "volume": 0.1,
        "tp": 0.0,
        "ticket": 601,
    }

    with patch.object(executor, "close_ticket", return_value=False) as mock_close:
        # Loop 1 at t=100.0: first attempt -> calls close_ticket, fails
        with patch("time.time", return_value=100.0):
            closed = executor.manage_stop_losses([position], account_balance=10000)
            assert closed == []
            assert mock_close.call_count == 1

        # Loop 2 at t=100.3 (300ms later): throttled -> must NOT call close_ticket
        with patch("time.time", return_value=100.3):
            closed = executor.manage_stop_losses([position], account_balance=10000)
            assert closed == []
            assert mock_close.call_count == 1  # Still 1! Not called!

        # Loop 3 at t=105.1 (after retry interval): retry triggered -> calls close_ticket again
        with patch("time.time", return_value=105.1):
            closed = executor.manage_stop_losses([position], account_balance=10000)
            assert closed == []
            assert mock_close.call_count == 2
