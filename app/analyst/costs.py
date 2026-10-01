"""Angel One delivery charges and slippage (rates from analyst.json "costs")."""


def _side(c: dict, n: float, buy: bool) -> float:
    """Exchange charges for one executed order of notional n, plus GST (DP is added by the caller)."""
    b = c["brokerage"]
    brokerage = max(min(b["flatInr"], b["pct"] * n), b["minInr"])
    exchange = n * (c["nseTxnPct"] + c["sebiPct"] + c["ipftPct"])
    return brokerage + exchange + c["gstPct"] * (brokerage + exchange) + n * c["sttPct"] + (n * c["stampBuyPct"] if buy else 0.0)


def buy_charges(c: dict, n: float) -> float:
    return _side(c, n, True)


def sell_charges(c: dict, n: float) -> float:
    """Sell side including the DP charge (one sell per scrip per day)."""
    return _side(c, n, False) + c["dpSellInr"] * (1 + c["dpGstPct"])


def slippage(c: dict, bucket: str, n: float) -> float:
    return c["slippageBpsPerSide"].get(bucket, 0) / 10000 * n


def round_trip(c: dict, bucket: str, n: float) -> float:
    """Buy + sell charges plus slippage on both sides."""
    return buy_charges(c, n) + sell_charges(c, n) + 2 * slippage(c, bucket, n)


def exit_cost(c: dict, bucket: str | None, qty: float, ltp: float) -> float:
    """Sell-side charges plus DP plus one side of slippage on the held quantity at ltp."""
    n = qty * ltp
    return sell_charges(c, n) + slippage(c, bucket or "", n)
