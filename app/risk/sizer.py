"""Sizer: weekly targets from the Analyst's selected names (risk-based, capped), KEEP resizing, DROPs and BUY funding."""

import math

import pandas as pd

from app.analyst import costs
from app.risk import monitor, stops, surveil
from app.risk.common import NO_TRADE_FLOOR_PCT, Context


def drop_reason(T: dict, bucket: str) -> str:
    """Why a held, no longer selected ticker is dropped (TDD "Delta against a book")."""
    entry, strategy = T["buckets"].get(bucket), (T["buckets"].get(bucket) or {}).get("strategy")
    if T["regime"]["active"] == "Unknown":
        return "UNKNOWN_REGIME"
    if entry is None or not strategy or strategy["top_n"] == 0 or T["composition"].get(bucket, 0) == 0:
        return "NO_ALLOCATION"
    return "NOT_SELECTED"


def targets(ctx: Context, nav: float, caps: dict) -> list[dict]:
    """Priced target per selected name, in funding order (bucket weight descending, rank ascending).

    N = riskPerPositionPct x NAV / s, capped per name, then scaled down so a bucket never exceeds weight x NAV x cap.
    """
    cfg, T = ctx.cfg, ctx.targets
    sz, sc = cfg["sizing"], cfg["stops"]
    order = sorted(T["buckets"], key=lambda b: -T["composition"].get(b, 0))  # stable: ties keep the file's order
    out = []
    for b in order:
        if b not in sc["clampPct"]:
            ctx.warn(f"UNKNOWN_BUCKET:{b}")
            continue
        lo, hi = sc["clampPct"][b]
        names = []
        for pick in sorted(T["buckets"][b]["selected"], key=lambda x: x["rank"]):
            t = pick["ticker"]
            df = ctx.hist(t)
            if df.empty or df.Date.iloc[-1] != ctx.asof:
                ctx.warn(f"NO_ROW:{t}")
                continue
            a = stops.adjusted(df)
            atr = stops.atr(a, sc["atrPeriod"]).iloc[-1]
            s = stops.width_pct(atr, a.C.iloc[-1], sc["atrMultiplier"], lo, hi)
            n = sz["riskPerPositionPct"] * nav / s
            cap = sz["nameCapPct"][b] * nav
            adv = float((df.Close * df.Volume).tail(cfg["liquidity"]["advDays"]).median())
            names.append({"ticker": t, "bucket": b, "rank": pick["rank"], "close": float(df.Close.iloc[-1]), "adj": float(df.AdjClose.iloc[-1]),
                          "s": float(s), "n": min(n, cap), "limitedBy": "NAME_CAP" if n > cap else "RISK_TARGET",
                          "advCap": cfg["liquidity"]["maxParticipationPct"][b] * adv})
        budget = T["composition"].get(b, 0) * nav * caps["finalCap"]
        total = sum(x["n"] for x in names)
        if total > budget:
            for x in names:
                x["n"], x["limitedBy"] = x["n"] * budget / total, "BUCKET_BUDGET"
        out += names
    return out


def plan_sells(ctx: Context, held: list[dict], st: dict, cands: dict, tgts: list[dict], nav: float) -> tuple[list, pd.DataFrame, dict]:
    """DROPs (with deferral), REBALANCE_TRIMs and the TOPUP shortfalls. Returns (holds, deferred table, {ticker: topup notional})."""
    sz, T = ctx.cfg["sizing"], ctx.targets
    selected = {x["ticker"] for b in T["buckets"].values() for x in b["selected"]}
    drops = {p["ticker"]: drop_reason(T, p["bucket"]) for p in held if p["ticker"] not in selected}
    holds, deferred = monitor.deferrals(ctx, held, st, cands, drops)
    by_ticker = {p["ticker"]: p for p in held}
    topups = {}
    for x in tgts:
        p = by_ticker.get(x["ticker"])
        if p is None or not p["has_row"] or p["ticker"] in cands:
            continue
        gap, target_w = x["n"] - p["value"], x["n"] / nav
        band = max(sz["noTradeBand"]["relative"] * target_w, sz["noTradeBand"]["absolutePct"], sz["noTradeBand"].get("floorPct", NO_TRADE_FLOOR_PCT))
        if abs(gap) / nav <= band or abs(gap) < sz["minAdjustmentInr"]:
            continue
        if gap < 0:
            qty = math.floor(-gap / p["close"])
            if qty >= 1 and qty * p["close"] >= sz["minAdjustmentInr"]:
                monitor.add(cands, p["ticker"], "REBALANCE_TRIM", qty, {"targetNotionalInr": round(x["n"], 2)})
        else:
            topups[p["ticker"]] = gap
    return holds, deferred, topups


def buys(ctx: Context, held: list[dict], sells: dict, cooldown: pd.DataFrame, tgts: list[dict], topups: dict, nav: float, cash: float, caps: dict) -> tuple[list, list, dict]:
    """BUY actions in funding order, blocked intents, and the heat / invested figures after all actions."""
    cfg, c = ctx.cfg, ctx.cfg["costs"]
    sz, surv = cfg["sizing"], ctx.surv or {}
    by_ticker = {p["ticker"]: p for p in held}
    left = {t: p["qty"] - sells[t]["qty"] if t in sells else p["qty"] for t, p in by_ticker.items()}
    proceeds = sum(s["qty"] * by_ticker[t]["close"] - costs.sell_charges(c, s["qty"] * by_ticker[t]["close"]) for t, s in sells.items())
    avail = cash - sz["cashBufferPct"] * nav + (proceeds if sz["countSaleProceeds"] else 0.0)
    invested = sum(left[t] * p["close"] for t, p in by_ticker.items())
    heat = sum(left[t] * p["close"] * max(p["adj"] - p["stop"], 0) / p["adj"] for t, p in by_ticker.items() if p["stop"] is not None)
    cool = cooldown.set_index("ticker")
    actions, blocked = [], []
    for x in tgts:
        t = x["ticker"]
        if t in sells or nav <= 0:
            continue
        kind = "TOPUP" if t in by_ticker else "ENTRY"
        if kind == "TOPUP" and t not in topups:
            continue
        block = None
        if kind == "ENTRY" and t in cool.index:
            r = cool.loc[t]
            if not (ctx.asof > r.release_after and (not cfg["cooldown"]["reentryAboveStopClose"] or x["adj"] > float(r.trigger_adj_close))):
                block = "COOLDOWN"
        if block is None and not surv.get("entries"):
            block = "NO_SURVEILLANCE_DATA"
        if block is None and surveil.entry_block(surv["data"], t, cfg):
            block = "SURVEILLANCE"
        if block:
            blocked.append({"ticker": t, "intent": kind, "reason": block})
            continue
        target = x["n"] if kind == "ENTRY" else topups[t]
        limits = {"TARGET": target, "ADV_CAP": x["advCap"], "INSUFFICIENT_CASH": avail - costs.buy_charges(c, max(avail, 0.0)),
                  "EXPOSURE_CAP": caps["finalCap"] * nav - invested, "HEAT_CAP": (cfg["heat"]["capPct"] * nav - heat) / x["s"]}
        bound = min(limits, key=limits.get)  # TARGET first on a tie
        n = max(0.0, limits[bound])
        qty = math.floor(n / x["close"])
        floor_n = sz["minNewOrderInr"] if kind == "ENTRY" else sz["minAdjustmentInr"]
        if qty == 0 or qty * x["close"] < floor_n:
            blocked.append({"ticker": t, "intent": kind, "reason": bound if bound != "TARGET" else "ZERO_QTY" if qty == 0 else "BELOW_MIN_NOTIONAL"})
            continue
        notional = qty * x["close"]
        avail -= notional + costs.buy_charges(c, notional)
        invested += notional
        heat += notional * x["s"]
        detail = {"rank": x["rank"], "stopWidthPct": round(x["s"], 4), "limitedBy": x["limitedBy"] if bound == "TARGET" else bound}
        if kind == "ENTRY":
            detail["stopPriceAtEntry"] = round(x["close"] * (1 - x["s"]), 2)
        actions.append({"ticker": t, "bucket": x["bucket"], "side": "BUY", "kind": kind, "qty": qty, "refPriceInr": round(x["close"], 2),
                        "notionalInr": round(notional, 2), "reason": kind, "estChargesInr": round(costs.buy_charges(c, notional), 2),
                        "priority": monitor.BUY_PRIORITY, "detail": detail})
    return actions, blocked, {"heatPct": heat / nav if nav > 0 else 0.0, "investedValue": invested}
