"""Risk Monitor: stop engine over the held positions, surveillance exits, exposure-cap reduction, deferred-DROP handling.

`cands` is the shared collection of SELL candidates, {ticker: [{"reason", "qty", "detail"}]}; `resolve` turns it into
one SELL per ticker (highest-priority reason, the rest in alsoTriggered).
"""

import math
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.market.common import iso
from app.risk import stops, surveil
from app.risk.common import Context, Portfolio, anniversary, is_date

PRIORITY = {"STOP": 1, "SURVEILLANCE": 2, "LADDER": 3, "REGIME_CAP": 3, "DROP_NOT_SELECTED": 4, "DROP_NO_ALLOCATION": 4,
            "DROP_UNKNOWN_REGIME": 4, "DROP_DEFERRED_RELEASED": 4, "REBALANCE_TRIM": 5}
BUY_PRIORITY = 6


def holdings(ctx: Context, pf: Portfolio, st: dict) -> tuple[list[dict], pd.DataFrame]:
    """One dict per book row (prices, stop, breach) and the updated positions table (track_start per held ticker)."""
    asof, sc = ctx.asof, ctx.cfg["stops"]
    old = st["positions"].set_index("ticker")
    cooldown = st["cooldown"].set_index("ticker")
    scan_after = ctx.last_good if ctx.last_good and ctx.last_good < asof else None
    held, rows = [], []
    for r in pf.book.itertuples():
        t = r.ticker
        bucket = ctx.bucket(t, old.bucket.get(t))
        known = is_date(r.entry_date)
        start = r.entry_date if known else (old.track_start.get(t) or asof)
        source = "ENTRY_DATE" if known else "FIRST_RUN"
        df = ctx.hist(t, iso(date.fromisoformat(start) - timedelta(days=60)))
        has_row = bool(len(df)) and df.Date.iloc[-1] == asof
        close = float(df.Close.iloc[-1]) if len(df) else float(r.avg_price)
        p = {"ticker": t, "bucket": bucket, "qty": int(r.qty), "avg": float(r.avg_price), "entry_date": r.entry_date,
             "entry_source": r.entry_source, "close": close, "adj": float(df.AdjClose.iloc[-1]) if len(df) else close,
             "prev_close": float(df.Close.iloc[-2]) if len(df) > 1 else None, "has_row": has_row, "value": r.qty * close,
             "track_start": start, "track_source": source, "stop": None, "hwm": None, "breach": None}
        if not has_row:
            ctx.warn(f"NO_ROW:{t}")
        else:
            lo, hi = sc["clampPct"][bucket]
            path = stops.stop_path(df, start, sc["atrMultiplier"], lo, hi, sc["atrPeriod"])
            p["stop"], p["hwm"] = float(path.stop.iloc[-1]), float(path.hwm.iloc[-1])
            if not np.isfinite(path.atr.iloc[-1]):
                ctx.warn(f"ATR_UNAVAILABLE:{t}")
            b = stops.first_breach(path, scan_after, asof)
            if b:
                p["breach"] = {**b, "lateBreach": b["breachDate"] < asof}
            elif t in cooldown.index and cooldown.trigger_date[t] >= start:  # a STOP repeats until the book no longer holds the ticker
                day = cooldown.trigger_date[t]
                at = path[path.Date == day].stop
                p["breach"] = {"breachDate": day, "stopPrice": float(at.iloc[0]) if len(at) else p["stop"], "lateBreach": False}
        held.append(p)
        rows.append({"ticker": t, "bucket": bucket, "track_start": start, "track_source": source, "last_seen": asof})
    return held, pd.DataFrame(rows, columns=["ticker", "bucket", "track_start", "track_source", "last_seen"])


def add(cands: dict, ticker: str, reason: str, qty: int, detail: dict | None = None) -> None:
    cands[ticker].append({"reason": reason, "qty": qty, "detail": detail or {}})


def exits(ctx: Context, held: list[dict], caps: dict, nav: float, cands: dict, info: dict) -> None:
    """STOP, SURVEILLANCE and exposure-cap SELL candidates; surveillance and circuit warnings."""
    cfg, s = ctx.cfg, (ctx.surv or {}).get("exits")
    for p in held:
        if not p["has_row"]:
            continue
        t = p["ticker"]
        if p["breach"]:
            add(cands, t, "STOP", p["qty"], p["breach"])
        if flag := surveil.exit_flag(s, t, cfg):
            add(cands, t, "SURVEILLANCE", p["qty"], {"list": flag})
        for w in surveil.warn_flags(s, t, cfg):
            ctx.warn(w)
        b = surveil.band(s, t)
        if b is not None and p["prev_close"] and p["close"] / p["prev_close"] - 1 <= -(b - 0.1) / 100:
            ctx.warn(f"LOWER_CIRCUIT_LIKELY:{t}")
    reduce_to_cap(ctx, held, caps, nav, cands, info)


def reduce_to_cap(ctx: Context, held: list[dict], caps: dict, nav: float, cands: dict, info: dict) -> None:
    """Sell, highest risk first, until invested value is within the final cap (whole names, then a trim of the last)."""
    qty = {p["ticker"]: p["qty"] for p in held}
    sold = {t for t, cs in cands.items() if any(c["qty"] >= qty[t] for c in cs)}
    excess = sum(p["value"] for p in held if p["ticker"] not in sold) - caps["finalCap"] * nav
    rank = {b: -i for i, b in enumerate(ctx.cfg["buckets"])}  # smaller caps first
    live = [p for p in held if p["ticker"] not in sold and p["has_row"]]
    live.sort(key=lambda p: (-p["value"] * (p["adj"] - p["stop"]) / p["adj"] / nav if nav > 0 else 0, rank[p["bucket"]], p["ticker"]))
    reason = caps["reason"]
    for p in live:
        if excess <= 1e-6:
            return
        if excess >= p["value"]:
            add(cands, p["ticker"], reason, p["qty"], info)
            excess -= p["value"]
            continue
        trim = math.ceil(excess / p["close"])
        if trim * p["close"] >= ctx.cfg["sizing"]["minAdjustmentInr"]:
            add(cands, p["ticker"], reason, min(trim, p["qty"]), info)
        else:
            ctx.warn(f"CAP_NOT_REACHED:{excess:.0f}")
        return
    if excess > 1e-6:
        ctx.warn(f"CAP_NOT_REACHED:{excess:.0f}")


def trend_ma(ctx: Context, ticker: str, window: int) -> float | None:
    df = ctx.hist(ticker, iso(date.fromisoformat(ctx.asof) - timedelta(days=int(window * 1.6) + 10)))
    return float(df.AdjClose.tail(window).mean()) if len(df) >= window else None


def deferral(ctx: Context, p: dict, row) -> tuple[str, dict]:
    """('HOLD' | 'RELEASE' | 'SELL', info) for a NOT_SELECTED name. row: its deferred_drops row, None for a new DROP.

    New deferral needs a known entry date, asOf within windowDays before the 12-month anniversary, an unrealised gain of
    at least minGainPct and AdjClose above the trend MA. An existing deferral lasts until the anniversary or a trend break.
    """
    d = ctx.cfg["tax"]["deferral"]
    if not d["enabled"] or not is_date(p["entry_date"]):
        return "SELL", {}
    asof = date.fromisoformat(ctx.asof)
    anniv = date.fromisoformat(row["anniversary"]) if row is not None else anniversary(date.fromisoformat(p["entry_date"]))
    gain = p["adj"] / p["avg"] - 1
    info = {"release": iso(anniv), "unrealisedGainPct": round(gain, 4)}
    window = ctx.windows.get(p["bucket"])
    ma = trend_ma(ctx, p["ticker"], window) if window else None
    trend_ok = not d["requireAboveTrend"] or (ma is not None and p["adj"] > ma)
    if row is not None:
        if asof >= anniv:
            return "RELEASE", info
        return ("HOLD", info) if trend_ok else ("SELL", {**info, "deferralEnded": "BELOW_TREND"})
    ok = 0 < (anniv - asof).days <= d["windowDays"] and gain >= d["minGainPct"] and trend_ok
    return ("HOLD" if ok else "SELL"), info


def deferrals(ctx: Context, held: list[dict], st: dict, cands: dict, drops: dict | None) -> tuple[list[dict], pd.DataFrame]:
    """HOLD_DEFERRED entries and the new deferred_drops table.

    drops: {ticker: DROP reason} on a rebalance day (a held name that is not in it was re-selected and leaves the file);
    None on other days, when only the names already in the file are re-checked. Non-NOT_SELECTED reasons are never deferred.
    """
    rows = st["deferred"].set_index("ticker")
    keep, holds = [], []
    for p in held:
        t = p["ticker"]
        row = rows.loc[t] if t in rows.index else None
        if (drops is None and row is None) or (drops is not None and t not in drops):
            continue
        if not p["has_row"]:
            if row is not None:
                keep.append({"ticker": t, **row.to_dict()})
            continue
        reason = drops[t] if drops is not None else "NOT_SELECTED"
        if reason != "NOT_SELECTED":
            add(cands, t, f"DROP_{reason}", p["qty"], {"dropReason": reason})
        elif t in cands:  # a STOP, ladder, regime-cap or surveillance exit sells it now, with that reason
            add(cands, t, "DROP_NOT_SELECTED", p["qty"], {"dropReason": "NOT_SELECTED"})
        else:
            decision, info = deferral(ctx, p, row)
            if decision == "HOLD":
                holds.append({"ticker": t, "bucket": p["bucket"], "action": "HOLD_DEFERRED", **info})
                keep.append({"ticker": t, **row.to_dict()} if row is not None else
                            {"ticker": t, "drop_date": ctx.asof, "anniversary": info["release"], "entry_date": p["entry_date"]})
            elif decision == "RELEASE":
                add(cands, t, "DROP_DEFERRED_RELEASED", p["qty"], info)
            else:
                add(cands, t, "DROP_NOT_SELECTED", p["qty"], {"dropReason": "NOT_SELECTED", **info})
    return holds, pd.DataFrame(keep, columns=["ticker", "drop_date", "anniversary", "entry_date"])


def resolve(cands: dict, held: dict) -> dict:
    """One SELL per ticker: {ticker: {reason, qty, kind, priority, detail}}; the largest quantity wins."""
    out = {}
    for t, cs in cands.items():
        cs = sorted(cs, key=lambda c: PRIORITY[c["reason"]])
        qty = min(max(c["qty"] for c in cs), held[t]["qty"])
        primary = cs[0]
        also = list(dict.fromkeys(c["reason"] for c in cs[1:] if c["reason"] != primary["reason"]))
        out[t] = {"reason": primary["reason"], "qty": qty, "kind": "EXIT" if qty >= held[t]["qty"] else "TRIM",
                  "priority": PRIORITY[primary["reason"]], "detail": {**primary["detail"], "alsoTriggered": also}}
    return out


def new_cands() -> dict:
    return defaultdict(list)
