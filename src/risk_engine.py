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

def trailing_threshold(peak_equity, start_balance, position_count, config):
    """
    Calculates the trailing threshold dollar amount.
    Tiered: 
      - If position_count > 1: Use averaging_risk_pct (default 15%)
      - If position_count == 1:
          - If profit > trailing_tighten_trigger_pct (default 5%), use trailing_tighten_risk_pct (default 5%)
          - Else use base_risk_pct (default 10%)
    Returns the threshold equity value (if equity drops below this, we stop out).
    """
    if position_count == 0:
        return 0.0
        
    # Defaults match discretionary scalper targets; prop-firm style accounts often use 2-5%.
    thresholds = config.get("thresholds", {})
    base_risk_pct = float(thresholds.get("base_risk_pct", 10.0))
    averaging_risk_pct = float(thresholds.get("averaging_risk_pct", 15.0))
    tighten_trigger = float(thresholds.get("trailing_tighten_trigger_pct", 5.0))
    tighten_risk = float(thresholds.get("trailing_tighten_risk_pct", 5.0))
    
    profit_pct = calculate_profit_pct(start_balance, peak_equity)
    
    if position_count > 1:
        risk_pct = averaging_risk_pct
    else:
        if profit_pct >= tighten_trigger:
            risk_pct = tighten_risk
        else:
            risk_pct = base_risk_pct
            
    risk_amount = peak_equity * (risk_pct / 100.0)
    return peak_equity - risk_amount

def daily_drawdown_breached(start_of_day_balance, current_equity, config):
    """
    Returns True if the current equity has dropped below the max daily drawdown percentage.
    """
    if start_of_day_balance <= 0:
        return False
        
    # Default 20%; prop-firm style accounts often cap daily loss at 5%.
    daily_dd_pct = float(config.get("thresholds", {}).get("daily_drawdown_pct", 20.0))
    threshold = start_of_day_balance * (1.0 - (daily_dd_pct / 100.0))
    
    return current_equity <= threshold
