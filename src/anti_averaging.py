import MetaTrader5 as mt5


def evaluate(positions, config):
    """
    Evaluates current positions against anti-averaging rules.
    Returns a list of position dictionaries that need to be flattened.
    """
    if not positions:
        return []

    anti_avg_config = config.get("anti_averaging", {})
    max_pos = int(anti_avg_config.get("max_positions_per_symbol", 2))
    max_vol = float(anti_avg_config.get("max_volume_per_symbol", 5.0))

    by_symbol = {}
    for p in positions:
        sym = p["symbol"]
        by_symbol.setdefault(sym, []).append(p)

    tickets_to_close = []

    for sym_positions in by_symbol.values():
        sorted_pos = sorted(sym_positions, key=lambda x: x["time"])

        if len(sorted_pos) > max_pos:
            excess = len(sorted_pos) - max_pos
            for p in sorted_pos[-excess:]:
                if p not in tickets_to_close:
                    tickets_to_close.append(p)

        total_vol = 0.0
        for p in sorted_pos:
            if p in tickets_to_close:
                continue
            total_vol += float(p["volume"])
            if total_vol > max_vol:
                if p not in tickets_to_close:
                    tickets_to_close.append(p)

        prevent_loser_add = anti_avg_config.get("prevent_loser_add", False)

        if prevent_loser_add:
            buys = [p for p in sorted_pos if p["type"] == mt5.ORDER_TYPE_BUY and p not in tickets_to_close]
            sells = [p for p in sorted_pos if p["type"] == mt5.ORDER_TYPE_SELL and p not in tickets_to_close]

            if len(buys) > 1:
                newest_buy = buys[-1]
                older_buys_profit = sum(p["profit"] for p in buys[:-1])
                # Only flag when older legs are net negative AND the add itself is in loss.
                if older_buys_profit < 0 and newest_buy["profit"] < 0:
                    tickets_to_close.append(newest_buy)

            if len(sells) > 1:
                newest_sell = sells[-1]
                older_sells_profit = sum(p["profit"] for p in sells[:-1])
                if older_sells_profit < 0 and newest_sell["profit"] < 0:
                    tickets_to_close.append(newest_sell)

    return tickets_to_close
