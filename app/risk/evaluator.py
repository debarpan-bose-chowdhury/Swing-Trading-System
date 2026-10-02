"""Evaluator statistics: performance, risk, benchmark, bear, regime, per-symbol, trade and rule figures (reporting only)."""

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.market.common import iso
from app.risk import nav as navmod
from app.risk.common import add_trading_days, read_json, read_table
from app.risk.tax import TRUSTED, fy

DAYS = 252
POSITION_COLS = ["date", "ticker", "qty", "close", "value", "day_pl", "day_return", "drawdown"]


def num(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def xirr(flows: list[tuple[str, float]]) -> float | None:
    """Annual rate r with sum(amount / (1 + r) ** (days / 365)) = 0, by bisection; None without a sign change."""
    t0 = date.fromisoformat(flows[0][0])
    f = lambda r: sum(a / (1 + r) ** ((date.fromisoformat(d) - t0).days / 365) for d, a in flows)  # noqa: E731
    lo, hi = -0.99, 100.0
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(lo) * f(mid) > 0 else (lo, mid)
    return (lo + hi) / 2


def drawdown_days(curve: pd.Series) -> int:
    """Longest peak-to-recovery spell in calendar days (an unrecovered drawdown runs to the last date)."""
    peak, peak_day, best = -math.inf, None, 0
    for day, v in curve.items():
        if v >= peak:
            best = max(best, (date.fromisoformat(day) - date.fromisoformat(peak_day)).days if peak_day and v > peak else 0)
            peak, peak_day = v, day
    if peak_day and curve.iloc[-1] < peak:
        best = max(best, (date.fromisoformat(curve.index[-1]) - date.fromisoformat(peak_day)).days)
    return best


def perf(r: pd.Series, rf: float, flows: list | None = None) -> dict:
    """Return, risk and risk-adjusted figures of a daily return series (index = ISO dates)."""
    r = r.dropna()
    if len(r) < 2:
        return {}
    curve = (1 + r).cumprod()
    dd = curve / curve.cummax() - 1
    total = float(curve.iloc[-1] - 1)
    cagr = (1 + total) ** (DAYS / len(r)) - 1 if total > -1 else None
    if flows:
        cagr = xirr(flows) if xirr(flows) is not None else cagr
    ex = r - rf / DAYS
    sd, down = r.std(ddof=1), math.sqrt(float((np.minimum(ex, 0) ** 2).mean()))
    tail = r[r <= r.quantile(0.05)]
    return {k: num(v) for k, v in {
        "totalReturn": total, "cagr": cagr, "volatility": sd * math.sqrt(DAYS), "maxDrawdown": dd.min(), "drawdownDurationDays": drawdown_days(curve),
        "ulcerIndex": math.sqrt(float(((dd * 100) ** 2).mean())), "cvar95": tail.mean(),
        "sharpe": ex.mean() / sd * math.sqrt(DAYS) if sd else None, "sortino": ex.mean() / down * math.sqrt(DAYS) if down else None,
        "calmar": cagr / abs(dd.min()) if cagr is not None and dd.min() < 0 else None}.items()}


def versus(r: pd.Series, b: pd.Series, rf: float) -> dict:
    """Beta, annualised Jensen's alpha and up / down capture against the benchmark (price index: alpha is flattered)."""
    j = pd.concat([r, b], axis=1, keys=["r", "b"]).dropna()
    if len(j) < 3 or j.b.var() == 0:
        return {}
    beta = j.r.cov(j.b) / j.b.var()
    d = rf / DAYS
    up, dn = j[j.b > 0], j[j.b < 0]
    return {k: num(v) for k, v in {"beta": beta, "alphaAnnual": ((j.r - d).mean() - beta * (j.b - d).mean()) * DAYS,
                                   "upCapture": up.r.mean() / up.b.mean() if len(up) else None,
                                   "downCapture": dn.r.mean() / dn.b.mean() if len(dn) else None}.items()}


def compound(r: pd.Series) -> float | None:
    return num((1 + r).prod() - 1) if len(r) else None


def regime_stats(df: pd.DataFrame, rf: float) -> dict:
    out = {}
    for name, g in df.groupby("regime"):
        p = perf(g.actual, rf)
        out[name] = {"days": len(g), "return": compound(g.actual), "volatility": p.get("volatility"), "maxDrawdown": p.get("maxDrawdown")}
    dominant = max(out, key=lambda k: out[k]["days"]) if out else None
    return {"byRegime": out, "dominant": dominant}


def frames(cfg: dict) -> pd.DataFrame:
    """Daily returns of actual, shadow and the benchmark with the active regime, indexed by date."""
    risk = Path(cfg["paths"]["risk"])
    a, s = navmod.read_nav(risk / "nav" / "nav_actual.csv"), navmod.read_nav(risk / "nav" / "nav_shadow.csv")
    df = pd.DataFrame({"actual": a.set_index("date").twr_index.pct_change(), "bench": a.set_index("date").bench_close.pct_change(),
                       "regime": a.set_index("date").active_regime})
    df["shadow"] = s.set_index("date").twr_index.pct_change() if len(s) else np.nan
    return df.iloc[1:]


def windows(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {"inception": df, "trailing252": df.tail(DAYS)}
    for name, key in (("fy", fy), ("cy", lambda d: d[:4])):
        for year, g in df.groupby(df.index.map(key)):
            out[f"{name}:{year}"] = g
    return out


def window_report(g: pd.DataFrame, navs: pd.DataFrame, cfg: dict) -> dict:
    ev = cfg["evaluator"]
    rf = ev["riskFreeRatePct"]
    flows = None
    rows = navs[navs.date.isin(g.index)]
    if len(rows) > 1 and (rows.flow.iloc[1:] != 0).any():  # XIRR from the NAV and the external flows of the window
        flows = [(rows.date.iloc[0], -float(rows.nav.iloc[0]))] + [(d, -float(f)) for d, f in zip(rows.date.iloc[1:], rows.flow.iloc[1:], strict=True) if f] + [(rows.date.iloc[-1], float(rows.nav.iloc[-1]))]
    out = {"observations": len(g), "flags": ["LOW_SAMPLE"] if len(g) < ev["minObs"] else [],
           "actual": {**perf(g.actual, rf, flows), **versus(g.actual, g.bench, rf)},
           "shadow": {**perf(g.shadow, rf), **versus(g.shadow, g.bench, rf)}, "benchmark": perf(g.bench, rf),
           "trackingGap": num(compound(g.actual) - compound(g.shadow)) if g.shadow.notna().any() and compound(g.actual) is not None else None}
    bear = g[g.regime == ev["bearRegime"]]
    out["bear"] = {"negativeDaysStrategy": int((g.actual < 0).sum()), "negativeDaysBenchmark": int((g.bench < 0).sum()), "bearDays": len(bear),
                   "strategyCompound": compound(bear.actual), "benchmarkCompound": compound(bear.bench)}
    out["regime"] = regime_stats(g, rf)
    return out


def per_symbol(positions: pd.DataFrame) -> list[dict]:
    rows = []
    for t, g in positions.groupby("ticker"):
        rows.append({"ticker": t, "daysHeld": len(g), "maxDrawdownFromHwm": num(g.drawdown.astype(float).min()),
                     "returnPct": compound(g.day_return.astype(float)), "plInr": round(float(g.day_pl.astype(float).sum()), 2)})
    rows.sort(key=lambda r: -r["plInr"])
    return [{**r, "plRank": i} for i, r in enumerate(rows, 1)]


def stop_signals(risk: Path) -> dict[str, list[dict]]:
    """ticker -> STOP signals (oldest first) from the signal files."""
    out = {}
    for f in sorted((risk / "signals").glob("signals_????-??-??.json")):
        for a in (read_json(f) or {}).get("actions", []):
            if a["reason"] == "STOP":
                out.setdefault(a["ticker"], []).append({"asOf": f.stem[8:], "stopPrice": a["detail"]["stopPrice"], "bucket": a["bucket"]})
    return out


def trade_stats(cfg: dict, cal, asof: str, navs: pd.DataFrame, journal: pd.DataFrame, fills: pd.DataFrame) -> dict:
    """Trades (journal), turnover and cost drag, and the rule figures: slippage beyond stop, whipsaw, time in market, adherence."""
    risk, analyst = Path(cfg["paths"]["risk"]), Path(cfg["paths"]["analyst"])
    j = journal[journal.source.isin(TRUSTED)].copy()
    pnl = j.net_pl.astype(float)
    wins, losses = pnl[pnl > 0], -pnl[pnl < 0]
    p = len(wins) / len(pnl) if len(pnl) else None
    avg_nav = float(navs.nav.mean()) if len(navs) else None
    traded = float((fills.qty.astype(float) * fills.price.astype(float))[fills.kind.isin(navmod.CASH_FILL_KINDS) & fills.side.isin(["BUY", "SELL"])].sum()) if len(fills) else 0.0
    trades = {"count": len(pnl), "hitRate": num(p), "payoffRatio": num(wins.mean() / losses.mean()) if len(wins) and len(losses) else None,
              "expectancyInr": num(p * wins.mean() - (1 - p) * losses.mean()) if p is not None and len(wins) and len(losses) else None,
              "turnover": num(traded / 2 / avg_nav) if avg_nav else None,
              "costDrag": num(j.est_charges.astype(float).sum() / avg_nav) if avg_nav and len(j) else None}
    stops, slip = stop_signals(risk), []
    for r in j.itertuples():
        prior = [s for s in stops.get(r.ticker, []) if s["asOf"] < r.exit_date]
        if prior:
            s = prior[-1]
            slip.append({"bucket": s["bucket"], "pct": (s["stopPrice"] - float(r.exit_price)) / s["stopPrice"]})
    sl = pd.DataFrame(slip, columns=["bucket", "pct"])
    stat = lambda x: {"count": len(x), "mean": num(x.mean()), "p95": num(x.quantile(0.95)), "p99": num(x.quantile(0.99))} if len(x) else {"count": 0}  # noqa: E731
    reselected = 0
    for t, sigs in stops.items():
        d0 = date.fromisoformat(sigs[0]["asOf"])
        for f in (analyst / "targets").glob("targets_????-??-??.json"):
            when, tf = f.stem[8:], read_json(f) or {}
            if d0 < date.fromisoformat(when) <= d0 + timedelta(weeks=4) and any(x["ticker"] == t for b in tf.get("buckets", {}).values() for x in b["selected"]):
                reselected += 1
                break
    near = date.fromisoformat(asof) - timedelta(days=7)
    sent = matched = 0
    for f in sorted((risk / "shadow" / "signals").glob("signals_????-??-??.json")):
        sig = read_json(f) or {}
        if date.fromisoformat(sig["asOf"]) <= near:
            continue
        end = iso(add_trading_days(cal, date.fromisoformat(sig["executionDate"]), 3))
        for a in sig["actions"]:
            sent += 1
            matched += bool(len(fills) and ((fills.ticker == a["ticker"]) & (fills.side == a["side"]) & (fills.trade_date >= sig["executionDate"]) & (fills.trade_date <= end)).any())
    rules = {"slippageBeyondStop": {"all": stat(sl.pct), **{b: stat(g.pct) for b, g in sl.groupby("bucket")}},
             "whipsawRate": num(reselected / len(stops)) if stops else None,
             "timeInMarket": num((navs.positions_value > 0).mean()) if len(navs) else None,
             "signalFollowedRate": num(matched / sent) if sent else None}
    return {"trades": trades, "rules": rules}


def build(cfg: dict, cal, asof: str, journal: pd.DataFrame, fills: pd.DataFrame, positions: pd.DataFrame) -> dict:
    """The weekly stats report: every window for actual, shadow and the benchmark, plus per-symbol, trade and rule figures."""
    navs = navmod.read_nav(Path(cfg["paths"]["risk"]) / "nav" / "nav_actual.csv")
    df = frames(cfg)
    return {"asOf": asof, "benchmark": cfg["evaluator"]["benchmark"],
            "benchmarkNote": f"{cfg['evaluator']['benchmark']} is a price index: alpha is flattered by the dividends it omits",
            "riskFreeRatePct": cfg["evaluator"]["riskFreeRatePct"], "windows": {k: window_report(g, navs, cfg) for k, g in windows(df).items()},
            "perSymbol": per_symbol(positions), **trade_stats(cfg, cal, asof, navs, journal, fills)}


def positions_rows(asof: str, held: list[dict], store, history) -> list[dict]:
    """positions_daily rows for the signal file's positions: value, day P/L, day return, drawdown from the high-water mark of raw closes."""
    rows = []
    for p in held:
        df = history(store, p["ticker"], p["trackStart"], asof)
        df = df[df.Date >= p["trackStart"]]
        if df.empty or df.Date.iloc[-1] != asof:
            continue
        close, prev = float(df.Close.iloc[-1]), float(df.Close.iloc[-2]) if len(df) > 1 else float(p["avgCostInr"])
        rows.append({"date": asof, "ticker": p["ticker"], "qty": p["qty"], "close": round(close, 2), "value": round(p["qty"] * close, 2),
                     "day_pl": round(p["qty"] * (close - prev), 2), "day_return": round(close / prev - 1, 6), "drawdown": round(close / float(df.Close.max()) - 1, 6)})
    return rows


def read_positions(path: Path) -> pd.DataFrame:
    return read_table(path, POSITION_COLS)
