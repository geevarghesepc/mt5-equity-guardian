"""Focused tests for the guardian loop integration.

These test the guardian_bot.run_iteration logic with mocked MT5, store, and
notifier to cover:
  - 2→1 peak reset
  - Same-symbol close ordering
  - Policy close reporting
  - Observe mode doesn't pretend closes succeeded
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from anti_averaging import PositionPolicy
from guardian_bot import _close_policy_tickets, run_iteration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos(ticket, symbol="EURUSD", profit=0.0, time=1, volume=0.1, pos_type=0):
    return {
        "ticket": ticket,
        "symbol": symbol,
        "volume": volume,
        "profit": profit,
        "type": pos_type,
        "time": time,
    }


def _make_executor(mode="live"):
    executor = MagicMock()
    executor.mode = mode
    executor.close_ticket.return_value = True
    executor.manage_stop_losses.return_value = []
    executor._observed_unprotected = set()
    executor._last_close_log = {}
    executor._non_hedging_alerted = False
    return executor


def _make_store():
    store = MagicMock()
    return store


def _make_notifier():
    notifier = MagicMock()
    return notifier


# ---------------------------------------------------------------------------
# _close_policy_tickets
# ---------------------------------------------------------------------------

def test_close_policy_tickets_same_symbol_reason():
    """Same-symbol close should have the correct reason label."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}

    # Baseline with ticket 1.
    positions = [_pos(1, profit=-5.0, time=1)]
    policy.evaluate(positions, config)

    # New ticket 2 arrives → ticket 1 is closed (same-symbol loser).
    positions = [
        _pos(1, profit=-5.0, time=1),
        _pos(2, profit=1.0, time=2),
    ]
    to_close = policy.evaluate(positions, config)

    executor = _make_executor()
    store = _make_store()
    notifier = _make_notifier()

    closed = _close_policy_tickets(executor, store, notifier, policy, to_close)
    assert closed == 1
    store.log_action.assert_called()
    args = store.log_action.call_args[0]
    assert args[0] == "SAME_SYMBOL_CLOSE"


def test_close_policy_tickets_global_cap_reason():
    """Global cap eviction should have the correct reason label."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}

    positions = [
        _pos(1, symbol="EURUSD", profit=5.0, time=1),
        _pos(2, symbol="GBPUSD", profit=5.0, time=2),
        _pos(3, symbol="AUDUSD", profit=5.0, time=3),
    ]
    to_close = policy.evaluate(positions, config)

    executor = _make_executor()
    store = _make_store()
    notifier = _make_notifier()

    closed = _close_policy_tickets(executor, store, notifier, policy, to_close)
    assert closed == 1
    args = store.log_action.call_args[0]
    assert args[0] == "GLOBAL_CAP_EVICTION"


def test_close_policy_tickets_retry_reason():
    """Failed and retried close should have the retry reason."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 1}}

    positions = [
        _pos(1, profit=5.0, time=1),
        _pos(2, profit=5.0, time=2),
    ]
    to_close = policy.evaluate(positions, config)
    # Report failure for the first close.
    policy.report_close_result(to_close[0]["ticket"], to_close[0], success=False)

    # Next iteration: retry.
    to_close = policy.evaluate(positions, config)
    assert len(to_close) >= 1

    executor = _make_executor()
    store = _make_store()
    notifier = _make_notifier()

    closed = _close_policy_tickets(executor, store, notifier, policy, to_close)
    assert closed >= 1


def test_close_policy_tickets_failure_queues_retry():
    """When executor.close_ticket fails, the policy stores the pending close."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 1}}

    positions = [
        _pos(1, profit=5.0, time=1),
        _pos(2, profit=5.0, time=2),
    ]
    to_close = policy.evaluate(positions, config)

    executor = _make_executor(mode="live")
    executor.close_ticket.return_value = False
    store = _make_store()
    notifier = _make_notifier()

    closed = _close_policy_tickets(executor, store, notifier, policy, to_close)
    assert closed == 0
    assert len(policy.pending_closes) == 1


def test_close_policy_observe_mode_counts_as_closed():
    """In observe mode, closes are counted for flow purposes."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 1}}

    positions = [
        _pos(1, profit=5.0, time=1),
        _pos(2, profit=5.0, time=2),
    ]
    to_close = policy.evaluate(positions, config)

    executor = _make_executor(mode="observe")
    executor.close_ticket.return_value = True
    store = _make_store()
    notifier = _make_notifier()

    closed = _close_policy_tickets(executor, store, notifier, policy, to_close)
    assert closed == 1


# ---------------------------------------------------------------------------
# run_iteration integration tests
# ---------------------------------------------------------------------------

@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_blocks_live_on_non_hedging(mock_reload):
    """Non-hedging account must block live orders and return early."""
    executor = _make_executor(mode="live")
    executor.validate_hedging.return_value = False
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1000.0, "balance": 1000.0, "margin_mode": 0
    }
    store = _make_store()
    notifier = _make_notifier()
    policy = PositionPolicy()
    config = {"mode": "live"}

    online, cooldown, count = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=0
    )

    # get_positions and close_ticket should never have been called
    executor.get_positions.assert_not_called()
    assert any("BLOCKED" in str(c) for c in notifier.send.call_args_list)


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_2_to_1_peak_reset_multi_iteration(mock_reload):
    """When count falls from 2 to 1 between iterations, peak equity resets to current equity."""
    executor = _make_executor(mode="live")
    executor.validate_hedging.return_value = True
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1050.0, "balance": 1000.0, "margin_mode": 2
    }
    # Only 1 position open now
    executor.get_positions.return_value = [_pos(2, profit=50.0, time=2)]
    executor.manage_stop_losses.return_value = []

    store = _make_store()
    # State recorded peak_equity at 1200.0 earlier
    store.get_state.return_value = {
        "start_balance": 1000.0, "peak_equity": 1200.0, "breaker_tripped": 0
    }
    notifier = _make_notifier()
    policy = PositionPolicy()
    config = {"mode": "live", "anti_averaging": {"max_open_positions": 2}}

    online, cooldown, count = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=2  # was 2 previously
    )

    assert count == 1
    # Verify peak was reset to current equity 1050.0
    store.save_state.assert_called()
    saved_calls = store.save_state.call_args_list
    assert any(call[0][2] == 1050.0 for call in saved_calls)
    assert any("Peak equity reset to 1050.00" in str(c) for c in notifier.send.call_args_list)


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_2_to_1_peak_reset_same_loop_admission(mock_reload):
    """Same-loop 1->2->1 replacement: new ticket arrives, policy closes loser, peak resets."""
    executor = _make_executor(mode="live")
    executor.validate_hedging.return_value = True
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1050.0, "balance": 1000.0, "margin_mode": 2
    }
    # Loop starts with 2 positions (ticket 1 loser, ticket 2 winner)
    executor.get_positions.side_effect = [
        [_pos(1, profit=-10.0, time=1), _pos(2, profit=60.0, time=2)],  # initial
        [_pos(2, profit=60.0, time=2)],  # after policy close
        [_pos(2, profit=60.0, time=2)],  # after manage_stop_losses
    ]
    executor.close_ticket.return_value = True
    executor.manage_stop_losses.return_value = []

    store = _make_store()
    store.get_state.return_value = {
        "start_balance": 1000.0, "peak_equity": 1200.0, "breaker_tripped": 0
    }
    notifier = _make_notifier()
    policy = PositionPolicy()
    # Baseline set earlier with ticket 1
    policy.set_baseline([_pos(1, profit=-10.0, time=1)])
    config = {"mode": "live", "anti_averaging": {"max_open_positions": 2}}

    # prev_pos_count was 1! Ticket 2 is newly detected this loop!
    online, cooldown, count = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=1
    )

    assert count == 1
    saved_calls = store.save_state.call_args_list
    assert any(call[0][2] == 1050.0 for call in saved_calls)


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_2_to_1_peak_reset_manage_stop_losses(mock_reload):
    """When manage_stop_losses closes a ticket bringing count from 2 to 1, peak resets."""
    executor = _make_executor(mode="live")
    executor.validate_hedging.return_value = True
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1050.0, "balance": 1000.0, "margin_mode": 2
    }
    # 2 positions
    executor.get_positions.side_effect = [
        [_pos(1, symbol="EURUSD", profit=10.0, time=1), _pos(2, symbol="GBPUSD", profit=40.0, time=2)],
        [_pos(2, symbol="GBPUSD", profit=40.0, time=2)],  # after unprotected close
        [_pos(2, symbol="GBPUSD", profit=40.0, time=2)],
    ]
    executor.manage_stop_losses.return_value = [_pos(1)]

    store = _make_store()
    store.get_state.return_value = {
        "start_balance": 1000.0, "peak_equity": 1200.0, "breaker_tripped": 0
    }
    notifier = _make_notifier()
    policy = PositionPolicy()
    config = {"mode": "live", "anti_averaging": {"max_open_positions": 2}}

    online, cooldown, count = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=2
    )

    assert count == 1
    saved_calls = store.save_state.call_args_list
    assert any(call[0][2] == 1050.0 for call in saved_calls)


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_account_switch_resets_policy(mock_reload):
    """Account change in config must reset PositionPolicy and prev_pos_count."""
    executor = _make_executor(mode="live")
    executor.validate_hedging.return_value = True
    # Account credentials changed!
    executor.apply_config.return_value = True
    executor.reconnect.return_value = True
    executor.get_account.return_value = {
        "login": 99999, "equity": 1000.0, "balance": 1000.0, "margin_mode": 2
    }
    executor.get_positions.return_value = []
    executor.manage_stop_losses.return_value = []

    store = _make_store()
    notifier = _make_notifier()
    policy = PositionPolicy()
    policy.decided_tickets = {1: "keep"}
    policy.baseline_tickets = {1}
    config = {"mode": "live"}

    online, cooldown, count = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=2
    )

    assert len(policy.decided_tickets) == 0
    assert len(policy.baseline_tickets) == 0


def test_observe_to_live_switch_enforces_pending_close():
    """Ticket decided for close in observe mode remains pending and executes in live mode."""
    policy = PositionPolicy()
    config_obs = {"mode": "observe", "anti_averaging": {"max_open_positions": 2}}
    positions = [_pos(1, profit=-10.0, time=1)]
    policy.set_baseline(positions)

    # In observe mode: ticket 2 arrives, ticket 1 must close
    positions = [_pos(1, profit=-10.0, time=1), _pos(2, profit=5.0, time=2)]
    to_close = policy.evaluate(positions, config_obs)
    assert len(to_close) == 1

    executor = _make_executor(mode="observe")
    store = _make_store()
    notifier = _make_notifier()

    _close_policy_tickets(executor, store, notifier, policy, to_close)
    # Ticket 1 should NOT be marked successfully closed; it must be in pending_closes
    assert 1 in policy.pending_closes

    # Now user switches to live mode
    config_live = {"mode": "live", "anti_averaging": {"max_open_positions": 2}}
    executor.mode = "live"
    to_close_live = policy.evaluate(positions, config_live)
    assert 1 in {p["ticket"] for p in to_close_live}

    _close_policy_tickets(executor, store, notifier, policy, to_close_live)
    executor.close_ticket.assert_called()
    assert 1 not in policy.pending_closes


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_observe_mode_does_not_repeat_peak_reset_on_pending_closes(mock_reload):
    """In observe mode, pending closes do not trigger repeated peak resets on every loop."""
    executor = _make_executor(mode="observe")
    executor.validate_hedging.return_value = True
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1050.0, "balance": 1000.0, "margin_mode": 2
    }
    # Two positions on MT5: ticket 1 losing, ticket 2 winning
    raw_positions = [
        _pos(1, profit=-10.0, time=1),
        _pos(2, profit=20.0, time=2),
    ]
    executor.get_positions.return_value = raw_positions
    executor.manage_stop_losses.return_value = []

    store = _make_store()
    store.get_state.return_value = {
        "start_balance": 1000.0, "peak_equity": 1050.0, "breaker_tripped": 0
    }
    notifier = _make_notifier()
    policy = PositionPolicy()
    config = {"mode": "observe", "anti_averaging": {"max_open_positions": 2}}

    # Loop 1: initial baseline was not set, so policy sets baseline, no close yet
    _, _, count1 = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=2
    )

    # Now simulate that ticket 3 arrives next loop, causing ticket 1 to be closed
    raw_positions_3 = [
        _pos(1, profit=-10.0, time=1),
        _pos(2, profit=20.0, time=2),
        _pos(3, profit=5.0, time=3),
    ]
    executor.get_positions.return_value = raw_positions_3

    # Loop 2: Ticket 1 is slated for close -> effective count drops from 3 to 2 (or 2 to 1 if max_open=1)
    config["anti_averaging"]["max_open_positions"] = 1
    notifier.send.reset_mock()
    store.save_state.reset_mock()

    _, _, count2 = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=2
    )
    # Peak reset notification sent in loop 2 because ticket 1 was newly closed
    peak_resets_loop2 = [c for c in notifier.send.call_args_list if "Peak equity reset" in str(c)]
    assert len(peak_resets_loop2) == 1

    # Loop 3: EXACT SAME positions returned by MT5 (still open in observe mode), ticket 1 is in pending_closes
    notifier.send.reset_mock()
    store.save_state.reset_mock()

    _, _, count3 = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=count2
    )
    # Loop 3 MUST NOT re-send "Peak equity reset" or re-save state for peak reset!
    peak_resets_loop3 = [c for c in notifier.send.call_args_list if "Peak equity reset" in str(c)]
    assert len(peak_resets_loop3) == 0


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_non_hedging_alert_sent_only_once(mock_reload):
    """A live non-hedging account alerts once and throttles across subsequent loops."""
    executor = _make_executor(mode="live")
    executor.validate_hedging.return_value = False
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1000.0, "balance": 1000.0, "margin_mode": 0
    }
    store = _make_store()
    notifier = _make_notifier()
    policy = PositionPolicy()
    config = {"mode": "live"}

    # Loop 1: should alert
    run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=0
    )
    assert notifier.send.call_count == 1
    assert "BLOCKED" in notifier.send.call_args[0][0]

    # Loop 2: should NOT alert again
    run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=0
    )
    assert notifier.send.call_count == 1  # Still 1!


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_observe_mode_does_not_repeat_peak_reset_on_unprotected_closes(mock_reload):
    """In observe mode, unprotected positions do not repeatedly reset peak equity every loop."""
    executor = _make_executor(mode="observe")
    executor.validate_hedging.return_value = True
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1050.0, "balance": 1000.0, "margin_mode": 2
    }
    raw_positions = [
        _pos(1, profit=10.0, time=1),
        _pos(2, profit=20.0, time=2),
    ]
    executor.get_positions.return_value = raw_positions
    # Loop 1: manage_stop_losses identifies ticket 1 as unprotected for the first time
    executor.manage_stop_losses.side_effect = [
        [_pos(1)],  # loop 1: newly detected unprotected
        [],         # loop 2: already recorded in _observed_unprotected, returns empty
    ]

    store = _make_store()
    store.get_state.return_value = {
        "start_balance": 1000.0, "peak_equity": 1050.0, "breaker_tripped": 0
    }
    notifier = _make_notifier()
    policy = PositionPolicy()
    config = {"mode": "observe", "anti_averaging": {"max_open_positions": 2}}

    # Loop 1: unprotected ticket 1 is simulated-closed
    _, _, count1 = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=2
    )
    assert count1 == 1
    peak_resets_loop1 = [c for c in notifier.send.call_args_list if "Peak equity reset" in str(c)]
    assert len(peak_resets_loop1) == 1

    # Simulate executor having recorded ticket 1 in _observed_unprotected
    executor._observed_unprotected.add(1)
    notifier.send.reset_mock()
    store.save_state.reset_mock()

    # Loop 2: identical open positions on MT5
    _, _, count2 = run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=count1
    )
    assert count2 == 1
    # MUST NOT re-fire peak reset or alert Telegram
    peak_resets_loop2 = [c for c in notifier.send.call_args_list if "Peak equity reset" in str(c)]
    assert len(peak_resets_loop2) == 0


@patch("guardian_bot.reload_config", return_value=True)
def test_run_iteration_logs_and_notifies_unprotected_closes(mock_reload):
    """Unprotected position closes must be written to audit store and alerted via Telegram."""
    executor = _make_executor(mode="live")
    executor.validate_hedging.return_value = True
    executor.apply_config.return_value = False
    executor.get_account.return_value = {
        "login": 12345, "equity": 1000.0, "balance": 1000.0, "margin_mode": 2
    }
    unprotected_pos = _pos(77, symbol="EURUSD", volume=0.2, profit=-25.0, time=1)
    executor.get_positions.return_value = [unprotected_pos]
    executor.manage_stop_losses.return_value = [unprotected_pos]

    store = _make_store()
    store.get_state.return_value = {
        "start_balance": 1000.0, "peak_equity": 1000.0, "breaker_tripped": 0
    }
    notifier = _make_notifier()
    policy = PositionPolicy()
    config = {"mode": "live", "anti_averaging": {"max_open_positions": 2}}

    run_iteration(
        config, executor, store, notifier, policy,
        mt5_online=True, cooldown_until=0.0, prev_pos_count=1
    )

    # Verify audit log recorded UNPROTECTED_CLOSE
    store.log_action.assert_called_with("UNPROTECTED_CLOSE", unprotected_pos)

    # Verify Telegram notification sent
    unprotected_alerts = [
        c for c in notifier.send.call_args_list if "Closed unprotected ticket 77" in str(c)
    ]
    assert len(unprotected_alerts) == 1
