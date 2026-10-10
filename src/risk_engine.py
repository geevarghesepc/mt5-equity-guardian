def update_peak_equity(current_peak, current_equity):
    """Returns the new peak equity, which is the max of the two."""
    if current_peak is None:
        return current_equity
    return max(current_peak, current_equity)


def calculate_profit_pct(start_balance, peak_equity):
    """Calculates the percentage profit of the peak equity compared to the start balance."""
    if start_balance <= 0:
        return 0.0
    return ((peak_equity - start_balance) / start_balance) * 100.0


def risk_pct_per_position(config):
    """Per-position risk as a percent of account balance.

    Reads ``thresholds.risk_pct_per_position`` (new key), falling back to the
    legacy ``thresholds.combined_risk_pct`` and finally a 5.0% default.
    """
    thresholds = config.get("thresholds", {})
    if "risk_pct_per_position" in thresholds:
        return float(thresholds["risk_pct_per_position"])
    # Legacy fallback — the old key was a combined budget; with the new
    # two-position policy each ticket gets the full amount individually.
    if "combined_risk_pct" in thresholds:
        return float(thresholds["combined_risk_pct"])
    return float(thresholds.get("base_risk_pct", 5.0))


def per_position_risk_amount(balance, config):
    """Dollar risk budget for a single ticket: ``balance * risk_pct / 100``."""
    pct = risk_pct_per_position(config)
    return float(balance) * (pct / 100.0)


# Kept for backward compat — older code imports this name.
def combined_risk_pct(config):
    """Combined risk across all open positions, as a percent of account size.

    With the new two-position policy every ticket gets its own 5% budget, so
    the combined exposure with two tickets is 10%.  This helper still returns
    the single-ticket percentage; callers that previously relied on a shared
    budget should switch to :func:`risk_pct_per_position`.
    """
    return risk_pct_per_position(config)


def allocate_risk_by_volume(positions, total_risk_amount):
    """Split a dollar risk budget across positions in proportion to volume.

    .. deprecated::
        With independent per-ticket risk, prefer
        :func:`per_position_risk_amount` for each ticket directly.
    """
    if not positions:
        return {}
    total_vol = sum(float(p.get("volume", 0.0)) for p in positions)
    if total_vol <= 0:
        share = total_risk_amount / len(positions)
        return {p["ticket"]: share for p in positions}
    return {
        p["ticket"]: total_risk_amount * (float(p["volume"]) / total_vol)
        for p in positions
    }


def trailing_threshold(peak_equity, account_size, position_count, config):
    """Equity level that trips the combined stop.

    Risk is ``risk_pct_per_position * min(position_count, max_open)`` of
    account balance.  The stop trails peak equity by that scaled dollar
    amount.  With two positions at 5 % each the combined allowance is 10 %.
    Zero positions → inactive (returns 0).
    """
    if position_count == 0:
        return 0.0

    max_open = int(
        config.get("anti_averaging", {}).get("max_open_positions", 2)
    )
    pct = risk_pct_per_position(config)
    capped_count = min(position_count, max_open)
    risk_amount = float(account_size) * (pct / 100.0) * capped_count
    return peak_equity - risk_amount


def daily_drawdown_breached(start_of_day_balance, current_equity, config):
    """
    Returns True if the current equity has dropped below the max daily drawdown percentage.
    """
    if start_of_day_balance <= 0:
        return False

    daily_dd_pct = float(config.get("thresholds", {}).get("daily_drawdown_pct", 20.0))
    threshold = start_of_day_balance * (1.0 - (daily_dd_pct / 100.0))

    return current_equity <= threshold
