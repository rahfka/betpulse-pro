def american_to_decimal(odds_us):
    val = float(odds_us)
    if val == 0:
        return None
    if val > 0:
        return round(1.0 + (val / 100.0), 4)
    return round(1.0 + (100.0 / abs(val)), 4)


def convert_odds(odds, from_format="american"):
    try:
        fmt = str(from_format).lower().strip()
        if fmt == "american":
            dec = american_to_decimal(odds)
        elif fmt == "decimal":
            dec = float(odds)
        else:
            dec = None

        if dec is None or dec <= 1.0:
            return {"data": {}}

        imp = round(1.0 / dec, 4)
        return {
            "data": {
                "decimal": round(dec, 2),
                "implied_probability": imp,
                "probability": imp,
            }
        }
    except Exception:
        return {"data": {}}


def find_edge(fair_prob, market_prob):
    try:
        fp = float(fair_prob)
        mp = float(market_prob)
        if mp <= 0.0:
            return {"data": {}}
        edge_pct = round(((fp - mp) / mp) * 100.0, 1)
        return {"data": {"ev_pct": edge_pct, "edge_pct": edge_pct, "edge": edge_pct}}
    except Exception:
        return {"data": {}}
