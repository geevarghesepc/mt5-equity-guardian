import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from risk_engine import (
    update_peak_equity,
    trailing_threshold,
    daily_drawdown_breached,
    calculate_profit_pct,
    allocate_risk_by_volume,
    combined_risk_pct,
    risk_pct_per_position,
    per_position_risk_amount,
)


# ---------------------------------------------------------------------------
# Peak equity
# ---------------------------------------------------------------------------

def test_update_peak_equity():
    assert update_peak_equity(None, 1000) == 1000
    assert update_peak_equity(1000, 1050) == 1050
    assert update_peak_equity(1050, 1020) == 1050


# ---------------------------------------------------------------------------
# risk_pct_per_position
# ---------------------------------------------------------------------------

def test_risk_pct_defaults_to_five():
    assert risk_pct_per_position({}) == 5.0


def test_risk_pct_reads_new_key():
    assert risk_pct_per_position({"thresholds": {"risk_pct_per_position": 3.0}}) == 3.0


def test_risk_pct_falls_back_to_combined():
    assert risk_pct_per_position({"thresholds": {"combined_risk_pct": 7.0}}) == 7.0


def test_risk_pct_falls_back_to_base():
    assert risk_pct_per_position({"thresholds": {"base_risk_pct": 8.0}}) == 8.0


def test_combined_risk_pct_alias():
    """Legacy combined_risk_pct is an alias for risk_pct_per_position."""
    assert combined_risk_pct({}) == 5.0
    assert combined_risk_pct({"thresholds": {"risk_pct_per_position": 3.0}}) == 3.0


# ---------------------------------------------------------------------------
# per_position_risk_amount
# ---------------------------------------------------------------------------

def test_per_position_risk_amount():
    config = {"thresholds": {"risk_pct_per_position": 5.0}}
    assert per_position_risk_amount(10000, config) == pytest.approx(500.0)


def test_per_position_risk_amount_different_pct():
    config = {"thresholds": {"risk_pct_per_position": 3.0}}
    assert per_position_risk_amount(10000, config) == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# Trailing threshold — scaled by position count
# ---------------------------------------------------------------------------

def test_trailing_threshold_one_position_five_percent():
    config = {"thresholds": {"risk_pct_per_position": 5.0}}
    # 1 position: 5% of 10000 = 500. Peak 10000 → threshold 9500.
    assert trailing_threshold(10000, 10000, 1, config) == pytest.approx(9500.0)


def test_trailing_threshold_two_positions_ten_percent():
    config = {"thresholds": {"risk_pct_per_position": 5.0}}
    # 2 positions: 5% × 2 = 10% of 10000 = 1000. Peak 10000 → threshold 9000.
    assert trailing_threshold(10000, 10000, 2, config) == pytest.approx(9000.0)


def test_trailing_threshold_capped_at_max_open():
    """Even with 5 positions, capped at max_open_positions=2."""
    config = {
        "thresholds": {"risk_pct_per_position": 5.0},
        "anti_averaging": {"max_open_positions": 2},
    }
    # Capped at 2: 5% × 2 = 10% of 10000 = 1000. Peak 10500 → threshold 9500.
    assert trailing_threshold(10500, 10000, 5, config) == pytest.approx(9500.0)


def test_trailing_threshold_trails_peak_by_scaled_risk():
    config = {"thresholds": {"risk_pct_per_position": 5.0}}
    # Peak 10500, account 10000, 2 positions: 10% = 1000 → threshold 9500.
    assert trailing_threshold(10500, 10000, 2, config) == pytest.approx(9500.0)


def test_trailing_threshold_zero_positions():
    assert trailing_threshold(1000, 1000, 0, {}) == 0.0


# ---------------------------------------------------------------------------
# Allocate risk by volume (legacy)
# ---------------------------------------------------------------------------

def test_allocate_risk_by_volume_splits_budget():
    positions = [
        {"ticket": 1, "volume": 0.1},
        {"ticket": 2, "volume": 0.3},
    ]
    allocated = allocate_risk_by_volume(positions, 400.0)
    assert allocated[1] == pytest.approx(100.0)
    assert allocated[2] == pytest.approx(300.0)


def test_allocate_risk_empty():
    assert allocate_risk_by_volume([], 500.0) == {}


def test_allocate_risk_zero_volume():
    positions = [
        {"ticket": 1, "volume": 0},
        {"ticket": 2, "volume": 0},
    ]
    allocated = allocate_risk_by_volume(positions, 400.0)
    assert allocated[1] == pytest.approx(200.0)
    assert allocated[2] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Daily drawdown
# ---------------------------------------------------------------------------

def test_daily_drawdown_breached():
    config = {"thresholds": {"daily_drawdown_pct": 20.0}}
    assert not daily_drawdown_breached(1000, 801, config)
    assert daily_drawdown_breached(1000, 800, config)
    assert daily_drawdown_breached(1000, 750, config)


def test_daily_drawdown_breached_zero_balance():
    assert not daily_drawdown_breached(0, -100, {})


# ---------------------------------------------------------------------------
# Profit pct
# ---------------------------------------------------------------------------

def test_calculate_profit_pct_zero_balance():
    assert calculate_profit_pct(0, 1000) == 0.0


def test_calculate_profit_pct_normal():
    assert calculate_profit_pct(10000, 10500) == pytest.approx(5.0)
