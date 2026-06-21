import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import MetaTrader5 as mt5
from anti_averaging import evaluate


def _pos(ticket, symbol="EURUSD", volume=0.1, profit=0.0, pos_type=mt5.ORDER_TYPE_BUY, time=1):
    return {
        "ticket": ticket,
        "symbol": symbol,
        "volume": volume,
        "profit": profit,
        "type": pos_type,
        "time": time,
    }


def test_no_positions_returns_empty():
    assert evaluate([], {"anti_averaging": {}}) == []


def test_max_positions_cap_closes_newest():
    config = {"anti_averaging": {"max_positions_per_symbol": 2, "max_volume_per_symbol": 10.0}}
    positions = [
        _pos(1, time=1),
        _pos(2, time=2),
        _pos(3, time=3),
    ]
    result = evaluate(positions, config)
    assert len(result) == 1
    assert result[0]["ticket"] == 3


def test_max_volume_cap_closes_overflow_position():
    config = {"anti_averaging": {"max_positions_per_symbol": 5, "max_volume_per_symbol": 0.15}}
    positions = [
        _pos(1, volume=0.1, time=1),
        _pos(2, volume=0.1, time=2),
    ]
    result = evaluate(positions, config)
    assert len(result) == 1
    assert result[0]["ticket"] == 2


def test_prevent_loser_add_only_when_newest_also_losing():
    config = {"anti_averaging": {"prevent_loser_add": True, "max_positions_per_symbol": 5, "max_volume_per_symbol": 5.0}}
    positions = [
        _pos(1, profit=-10.0, time=1),
        _pos(2, profit=5.0, time=2),
    ]
    result = evaluate(positions, config)
    assert result == []


def test_prevent_loser_add_flags_newest_when_both_sides_losing():
    config = {"anti_averaging": {"prevent_loser_add": True, "max_positions_per_symbol": 5, "max_volume_per_symbol": 5.0}}
    positions = [
        _pos(1, profit=-10.0, time=1),
        _pos(2, profit=-2.0, time=2),
    ]
    result = evaluate(positions, config)
    assert len(result) == 1
    assert result[0]["ticket"] == 2


def test_prevent_loser_add_sells_flags_newest_when_both_sides_losing():
    config = {"anti_averaging": {"prevent_loser_add": True, "max_positions_per_symbol": 5, "max_volume_per_symbol": 5.0}}
    positions = [
        _pos(1, profit=-10.0, pos_type=mt5.ORDER_TYPE_SELL, time=1),
        _pos(2, profit=-2.0, pos_type=mt5.ORDER_TYPE_SELL, time=2),
    ]
    result = evaluate(positions, config)
    assert len(result) == 1
    assert result[0]["ticket"] == 2


def test_prevent_loser_add_sells_only_when_newest_also_losing():
    config = {"anti_averaging": {"prevent_loser_add": True, "max_positions_per_symbol": 5, "max_volume_per_symbol": 5.0}}
    positions = [
        _pos(1, profit=-10.0, pos_type=mt5.ORDER_TYPE_SELL, time=1),
        _pos(2, profit=5.0, pos_type=mt5.ORDER_TYPE_SELL, time=2),
    ]
    result = evaluate(positions, config)
    assert result == []
