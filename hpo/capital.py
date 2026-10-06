"""The smallest account the live sizing can trade: the sizer's arithmetic gate, solved for capital.

A new position (an ENTRY) is placed only if floor(n / price) x price >= sizing.minNewOrderInr, where, per name (app/risk/sizer.py):
    n = min( riskPerPositionPct x NAV / s ,  nameCapPct[bucket] x NAV )         s = stop width, clamped to clampPct[bucket] = [lo, hi]
    n is scaled down to weight x NAV when top_n names of the bucket together would exceed the bucket's budget (composition weight x NAV)
    the other limits (cash, the exposure cap, the heat cap, the volume cap) only bind later or on bigger accounts.
Best case for a name: s at its floor `lo`, nothing else limiting, the full exposure cap. So n / NAV = min(riskPerPositionPct / lo, nameCap, weight / top_n) and the account must satisfy
    NAV >= (minNewOrderInr + price) / (n / NAV)      (the share price is the rounding slack: whole shares round the order down by up to one share)
This is a lower bound: a name with a wider stop, a lower exposure cap (after a drawdown) or fewer selected names than top_n changes it, the first two upward.
"""

import math

REGIMES = ("BULL", "TREND", "WEAK", "BEAR")


def fractions(risk: dict, analyst: dict) -> list[dict]:
    """Per bucket and regime: the share of NAV one entry can reach at best, and why it stops there."""
    sz, comp = risk["sizing"], analyst["composition"]
    rows = []
    for regime in REGIMES:
        for bucket, w in comp.items():
            k = analyst["strategies"][regime][bucket]["top_n"]
            if w <= 0 or k <= 0:
                continue
            lo = risk["stops"]["clampPct"][bucket][0]
            limits = {"RISK_TARGET": sz["riskPerPositionPct"] / lo, "NAME_CAP": sz["nameCapPct"][bucket], "BUCKET_BUDGET": w / k}
            why = min(limits, key=limits.get)
            rows.append({"regime": regime, "bucket": bucket, "names": k, "fraction": limits[why], "limitedBy": why})
    return rows


def required(risk: dict, analyst: dict, price: float) -> dict:
    """Capital needed for one entry to clear the minimum order, per bucket and regime, and the account sizes that matter."""
    minimum = risk["sizing"]["minNewOrderInr"]
    rows = [{**r, "needed": (minimum + price) / r["fraction"], "withoutRounding": minimum / r["fraction"]} for r in fractions(risk, analyst)]
    best = {reg: min((r for r in rows if r["regime"] == reg), key=lambda r: r["needed"], default=None) for reg in REGIMES}
    live = [b for b in best.values() if b]
    return {"minimumOrder": minimum, "price": price, "rows": rows, "bestPerRegime": best, "anyRegime": min(r["needed"] for r in rows) if rows else None,
            "everyRegime": max(b["needed"] for b in live) if live else None, "namecapOnly": minimum / max(risk["sizing"]["nameCapPct"].values())}


def round_up(x: float, step: float = 50_000.0) -> int:
    return int(math.ceil(x / step) * step)
