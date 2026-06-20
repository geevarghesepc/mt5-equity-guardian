import pytest
import sys
import os

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from risk_engine import update_peak_equity, trailing_threshold, daily_drawdown_breached

def test_update_peak_equity():
    assert update_peak_equity(None, 1000) == 1000
    assert update_peak_equity(1000, 1050) == 1050
    assert update_peak_equity(1050, 1020) == 1050

def test_trailing_threshold_base():
    config = {"thresholds": {"base_risk_pct": 10.0}}
    # 10% risk of 1000 is 100. Threshold is 900.
    assert trailing_threshold(1000, 1000, 1, config) == 900.0

def test_trailing_threshold_averaging():
    config = {"thresholds": {"averaging_risk_pct": 15.0}}
    # 15% risk of 1000 is 150. Threshold is 850.
    assert trailing_threshold(1000, 1000, 2, config) == 850.0
    
def test_trailing_threshold_tighten():
    config = {
        "thresholds": {
            "base_risk_pct": 10.0,
            "trailing_tighten_trigger_pct": 5.0,
            "trailing_tighten_risk_pct": 5.0
        }
    }
    # Profit is exactly 5% (1050 vs 1000). Should use tighten risk (5%).
    # 5% of 1050 is 52.5. Threshold is 1050 - 52.5 = 997.5
    assert trailing_threshold(1050, 1000, 1, config) == 997.5

def test_daily_drawdown_breached():
    config = {"thresholds": {"daily_drawdown_pct": 20.0}}
    # 20% DD on 1000 is drop to 800.
    assert not daily_drawdown_breached(1000, 801, config)
    assert daily_drawdown_breached(1000, 800, config)
    assert daily_drawdown_breached(1000, 750, config)
