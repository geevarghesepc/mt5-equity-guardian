import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from anti_averaging import PositionPolicy, evaluate


def _pos(ticket, symbol="EURUSD", volume=0.1, profit=0.0, pos_type=None, time=1):
    return {
        "ticket": ticket,
        "symbol": symbol,
        "volume": volume,
        "profit": profit,
        "type": pos_type if pos_type is not None else 0,
        "time": time,
    }


# ---------------------------------------------------------------------------
# Basic / empty
# ---------------------------------------------------------------------------

def test_no_positions_returns_empty():
    policy = PositionPolicy()
    assert policy.evaluate([], {"anti_averaging": {}}) == []


def test_single_position_is_kept():
    policy = PositionPolicy()
    positions = [_pos(1, profit=-10.0, time=1)]
    result = policy.evaluate(positions, {"anti_averaging": {"max_open_positions": 2}})
    assert result == []


# ---------------------------------------------------------------------------
# Same-symbol: close old loser on new add
# ---------------------------------------------------------------------------

def test_new_add_closes_old_loser_same_symbol():
    """New ticket on same symbol → close older if profit <= 0."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    # First iteration: baseline with ticket 1.
    positions = [_pos(1, profit=-10.0, time=1)]
    policy.evaluate(positions, config)

    # Second iteration: ticket 2 appears (same symbol).
    positions = [
        _pos(1, profit=-10.0, time=1),
        _pos(2, profit=1.0, time=2),
    ]
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    assert 1 in tickets, "Old loser should be closed"


def test_pyramid_keeps_old_winner_same_symbol():
    """New ticket on same symbol → keep older if profit > 0."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    positions = [_pos(1, profit=10.0, time=1)]
    policy.evaluate(positions, config)

    positions = [
        _pos(1, profit=10.0, time=1),
        _pos(2, profit=-1.0, time=2),
    ]
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    assert 1 not in tickets, "Old winner should be kept"


def test_breakeven_is_rejected():
    """Profit == 0 should be treated as <= 0 → close."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    positions = [_pos(1, profit=0.0, time=1)]
    policy.evaluate(positions, config)

    positions = [
        _pos(1, profit=0.0, time=1),
        _pos(2, profit=1.0, time=2),
    ]
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    assert 1 in tickets


# ---------------------------------------------------------------------------
# Cross-symbol isolation
# ---------------------------------------------------------------------------

def test_cross_symbol_no_same_symbol_check():
    """Tickets on different symbols should not trigger same-symbol admission."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    positions = [_pos(1, symbol="EURUSD", profit=-5.0, time=1)]
    policy.evaluate(positions, config)

    # New ticket on a different symbol.
    positions = [
        _pos(1, symbol="EURUSD", profit=-5.0, time=1),
        _pos(2, symbol="GBPUSD", profit=1.0, time=2),
    ]
    result = policy.evaluate(positions, config)
    # Both are within cap of 2 and different symbols — nothing to close.
    assert result == []


# ---------------------------------------------------------------------------
# Same-symbol precedence: before global cap
# ---------------------------------------------------------------------------

def test_same_symbol_before_global_cap():
    """Same-symbol close happens before the global cap check."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    positions = [
        _pos(1, symbol="EURUSD", profit=5.0, time=1),
        _pos(2, symbol="GBPUSD", profit=5.0, time=2),
    ]
    policy.evaluate(positions, config)

    # Third ticket on EURUSD while already at cap.
    positions = [
        _pos(1, symbol="EURUSD", profit=-5.0, time=1),
        _pos(2, symbol="GBPUSD", profit=5.0, time=2),
        _pos(3, symbol="EURUSD", profit=0.5, time=3),
    ]
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    # Ticket 1 is closed via same-symbol (losing).
    # That brings count to 2, so no global cap eviction needed.
    assert 1 in tickets
    # Only one close required.
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Global cap: oldest-first eviction
# ---------------------------------------------------------------------------

def test_global_cap_oldest_first():
    """When >2 positions, oldest by (time, ticket) is evicted first."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    # All three appear at once (as if guardian starts with 3 already open).
    positions = [
        _pos(10, symbol="EURUSD", profit=5.0, time=100),
        _pos(20, symbol="GBPUSD", profit=5.0, time=200),
        _pos(30, symbol="AUDUSD", profit=5.0, time=300),
    ]
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    assert 10 in tickets, "Oldest ticket should be evicted"
    assert len(result) == 1


def test_global_cap_deterministic_tie_break():
    """Same open time — break ties by ticket number."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    positions = [
        _pos(30, symbol="EURUSD", profit=5.0, time=100),
        _pos(10, symbol="GBPUSD", profit=5.0, time=100),
        _pos(20, symbol="AUDUSD", profit=5.0, time=100),
    ]
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    # Ticket 10 has the smallest ticket number at the same time.
    assert 10 in tickets


# ---------------------------------------------------------------------------
# Close retries
# ---------------------------------------------------------------------------

def test_close_failure_retries_next_iteration():
    """Failed closes are retried on the next evaluation."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 1}}
    positions = [
        _pos(1, profit=5.0, time=1),
        _pos(2, profit=5.0, time=2),
    ]
    result = policy.evaluate(positions, config)
    assert len(result) == 1
    closed_ticket = result[0]["ticket"]

    # Report failure.
    policy.report_close_result(closed_ticket, result[0], success=False)

    # Next iteration — same positions still open.
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    assert closed_ticket in tickets, "Failed close should be retried"


def test_close_success_removes_pending():
    """Successful closes are not retried."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 1}}
    positions = [
        _pos(1, profit=5.0, time=1),
        _pos(2, profit=5.0, time=2),
    ]
    result = policy.evaluate(positions, config)
    closed_ticket = result[0]["ticket"]

    policy.report_close_result(closed_ticket, result[0], success=True)
    assert closed_ticket not in policy.pending_closes


# ---------------------------------------------------------------------------
# Baseline tickets
# ---------------------------------------------------------------------------

def test_baseline_tickets_exempt_from_same_symbol_test():
    """Tickets present at first observation are not tested for profit against each other."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    # First observation: two tickets already open on EURUSD, ticket 1 losing.
    positions = [
        _pos(1, symbol="EURUSD", profit=-10.0, time=1),
        _pos(2, symbol="EURUSD", profit=1.0, time=2),
    ]
    result = policy.evaluate(positions, config)
    assert 1 in policy.baseline_tickets
    assert 2 in policy.baseline_tickets
    # Both are baseline tickets; neither was added afterward, so neither is closed.
    assert result == []


def test_baseline_tickets_still_subject_to_global_cap():
    """Baseline tickets can still be evicted by the global cap."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}
    # Three tickets as baseline.
    positions = [
        _pos(1, symbol="EURUSD", profit=-10.0, time=1),
        _pos(2, symbol="GBPUSD", profit=-10.0, time=2),
        _pos(3, symbol="AUDUSD", profit=-10.0, time=3),
    ]
    result = policy.evaluate(positions, config)
    assert len(result) == 1, "One ticket should be evicted by global cap"
    assert result[0]["ticket"] == 1, "Oldest baseline ticket is evicted"


# ---------------------------------------------------------------------------
# Decision finality
# ---------------------------------------------------------------------------

def test_accepted_ticket_not_closed_when_profit_turns_negative():
    """An accepted (keep) ticket is never later closed by same-symbol logic."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}

    # Baseline with ticket 1 winning.
    positions = [_pos(1, symbol="EURUSD", profit=10.0, time=1)]
    policy.evaluate(positions, config)

    # Ticket 2 arrives; ticket 1 is baseline (exempt).
    positions = [
        _pos(1, symbol="EURUSD", profit=10.0, time=1),
        _pos(2, symbol="EURUSD", profit=0.5, time=2),
    ]
    policy.evaluate(positions, config)

    # Ticket 1's profit turns negative.
    positions = [
        _pos(1, symbol="EURUSD", profit=-5.0, time=1),
        _pos(2, symbol="EURUSD", profit=0.5, time=2),
    ]
    result = policy.evaluate(positions, config)
    tickets = {p["ticket"] for p in result}
    assert 1 not in tickets, "Already-decided ticket should not be re-closed"


# ---------------------------------------------------------------------------
# Legacy compat wrapper
# ---------------------------------------------------------------------------

def test_legacy_evaluate_still_works():
    """The module-level ``evaluate()`` should still return sensible results."""
    config = {"anti_averaging": {"max_open_positions": 2}}
    positions = [
        _pos(1, profit=-10.0, time=1),
        _pos(2, profit=-10.0, time=2),
        _pos(3, profit=5.0, time=3),
    ]
    result = evaluate(positions, config)
    # With an ephemeral policy (no baseline), the oldest is evicted by cap.
    assert len(result) >= 1


# ---------------------------------------------------------------------------
# Edge: no anti_averaging section in config
# ---------------------------------------------------------------------------

def test_missing_config_section_uses_defaults():
    policy = PositionPolicy()
    positions = [
        _pos(1, profit=5.0, time=1),
        _pos(2, profit=5.0, time=2),
        _pos(3, profit=5.0, time=3),
    ]
    result = policy.evaluate(positions, {})
    # Default max_open is 2 → one eviction.
    assert len(result) == 1


def test_pending_closes_snapshot_refreshed_with_live_position_data():
    """Retry closes must use the updated live position snapshot rather than stale data."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 1}}
    initial_pos = _pos(10, profit=-2.0, time=1, volume=0.1)
    other_pos = _pos(20, profit=5.0, time=2, volume=0.2)

    # Initial evaluation decides ticket 10 to close
    to_close = policy.evaluate([initial_pos, other_pos], config)
    assert len(to_close) == 1
    assert to_close[0]["ticket"] == 10
    assert to_close[0]["profit"] == -2.0

    # Simulate close failure -> stored in pending_closes
    policy.report_close_result(10, initial_pos, success=False)
    assert policy.pending_closes[10]["profit"] == -2.0

    # On next loop, ticket 10's live floating profit and volume have changed
    updated_pos = _pos(10, profit=-15.0, time=1, volume=0.15)
    retry_to_close = policy.evaluate([updated_pos, other_pos], config)

    assert len(retry_to_close) == 1
    assert retry_to_close[0]["ticket"] == 10
    assert retry_to_close[0]["profit"] == -15.0
    assert retry_to_close[0]["volume"] == 0.15
    assert policy.pending_closes[10]["profit"] == -15.0
    assert policy.pending_closes[10]["volume"] == 0.15


def test_failed_same_symbol_close_does_not_overclose_via_global_cap():
    """Pending close from same-symbol failure must not cause global cap to close surviving position."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}

    # Baseline has GBP (ticket 1) and EUR loser (ticket 2)
    pos_gbp = _pos(1, symbol="GBPUSD", profit=10.0, time=1)
    pos_eur_loser = _pos(2, symbol="EURUSD", profit=-5.0, time=2)
    policy.evaluate([pos_gbp, pos_eur_loser], config)

    # New EUR ticket 3 arrives
    pos_eur_new = _pos(3, symbol="EURUSD", profit=1.0, time=3)
    positions = [pos_gbp, pos_eur_loser, pos_eur_new]
    to_close = policy.evaluate(positions, config)

    # Loop 1: Ticket 2 is selected by same-symbol admission
    assert [p["ticket"] for p in to_close] == [2]

    # Ticket 2 close fails
    policy.report_close_result(2, pos_eur_loser, success=False)
    assert 2 in policy.pending_closes

    # Loop 2: Same 3 positions are still open, Ticket 2 is pending retry.
    # GBP (ticket 1) must NOT be evicted by global cap; only Ticket 2 retry should be returned.
    next_to_close = policy.evaluate(positions, config)
    closed_tickets = [p["ticket"] for p in next_to_close]
    assert closed_tickets == [2], f"Expected only ticket 2 retry, but got {closed_tickets}"
    assert 1 not in closed_tickets, "Surviving oldest position (ticket 1) must not be over-closed"


def test_retained_ticket_re_evaluated_on_later_same_symbol_add():
    """A ticket kept on earlier add must be re-evaluated if a later add occurs and it is now losing."""
    policy = PositionPolicy()
    config = {"anti_averaging": {"max_open_positions": 2}}

    # Ticket 1 starts as a winner
    pos1 = _pos(1, symbol="EURUSD", profit=15.0, time=1)
    policy.evaluate([pos1], config)

    # Ticket 2 arrives: Ticket 1 is winning, so it is kept
    pos2 = _pos(2, symbol="EURUSD", profit=2.0, time=2)
    to_close = policy.evaluate([pos1, pos2], config)
    assert to_close == [], "Ticket 1 kept because profit > 0"
    assert policy.decided_pairs.get((1, 2)) == "keep"

    # Ticket 2 closes. Ticket 1's profit reverses into a loss.
    pos1_losing = _pos(1, symbol="EURUSD", profit=-8.0, time=1)
    to_close = policy.evaluate([pos1_losing], config)
    assert to_close == [], "No new ticket, so no admission check"

    # Later, Ticket 3 arrives on EURUSD. Ticket 1 is now losing.
    pos3 = _pos(3, symbol="EURUSD", profit=0.5, time=3)
    to_close = policy.evaluate([pos1_losing, pos3], config)

    # Ticket 1 must now be closed because it is losing on the new add!
    assert len(to_close) == 1
    assert to_close[0]["ticket"] == 1
    assert policy.decided_pairs.get((1, 3)) == "close"


def test_report_close_result_throttles_warning_logs(caplog):
    """Initial close failure logs warning; repeated failures for same ticket log debug only."""
    import logging
    policy = PositionPolicy()
    pos = _pos(5, symbol="EURUSD", profit=-5.0, time=1)

    with caplog.at_level(logging.DEBUG):
        # First failure -> logs warning
        policy.report_close_result(5, pos, success=False)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "ticket 5" in r.message]
        assert len(warnings) == 1

        caplog.clear()

        # Second failure for same ticket -> logs debug, NOT warning
        policy.report_close_result(5, pos, success=False)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "ticket 5" in r.message]
        assert len(warnings) == 0
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "ticket 5" in r.message]
        assert len(debugs) == 1
