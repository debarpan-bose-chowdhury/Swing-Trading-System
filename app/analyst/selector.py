"""Per-bucket stock selection for one rebalance date: liquidity, trend, momentum, and the BEAR composite ranking."""

from datetime import date
from pathlib import Path

import pandas as pd

from app.market.store import Store

BEAR_ROWS = 70  # the composite score needs 63 days of history plus the 20-day return window
CRORE = 1e7


def rows_needed(cfg: dict) -> int:
    """Rows of history to load per ticker: the longest window any strategy, the liquidity check or the BEAR score needs."""
    sel = cfg["selector"]
    strategies = [s for per_bucket in cfg["strategies"].values() for s in per_bucket.values()]
    need = max(max(s["stock_trend_ma"] + 5, s["lookback"] + sel["momentumSkipDays"] + 2) for s in strategies)
    return max(need, BEAR_ROWS, sel["liquidity"]["windowDays"]) + 5


def bucket_universe(cfg: dict, bucket: str, rebalance: str) -> tuple[str, list[str]]:
    """Symbols of the newest bucket file dated on or before the rebalance date; fails when it is too old."""
    files = sorted((f.stem.rsplit("_", 1)[1], f) for f in Path(cfg["paths"]["upstreamStorage"]).glob(f"{bucket}_*.csv"))
    files = [(day, f) for day, f in files if day <= rebalance]
    if not files:
        raise ValueError(f"no {bucket} bucket file on or before {rebalance}")
    day, path = files[-1]
    if (date.fromisoformat(rebalance) - date.fromisoformat(day)).days > cfg["selector"]["maxBucketFileAgeDays"]:
        raise ValueError(f"{bucket} bucket file {day} is older than {cfg['selector']['maxBucketFileAgeDays']} days")
    symbols = pd.read_csv(path, dtype=str, keep_default_na=False)["Symbol"]
    return day, sorted(set(symbols) - {""})


def load_panel(store: Store, symbols: list[str], rebalance: str, rows: int) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Date-by-ticker AdjClose and traded value (raw Close x Volume), rows up to the rebalance date, no fill.

    The archive tier is read only for tickers whose fresh file is too short. Returns (adj, value, tickers without data).
    """
    adj, value = {}, {}
    for sym in symbols:
        df = store.read_fresh(sym)
        df = df[df.Date <= rebalance]
        if len(df) < rows:
            old = store.read_archive(sym)
            df = pd.concat([old[old.Date <= rebalance], df]).drop_duplicates("Date", keep="last")
        df = df.sort_values("Date").tail(rows)
        if df.empty:
            continue
        idx = pd.DatetimeIndex(df.Date)
        adj[sym] = pd.Series(df.AdjClose.to_numpy(float), index=idx)
        value[sym] = pd.Series((df.Close * df.Volume).to_numpy(float), index=idx)
    return pd.DataFrame(adj).sort_index(), pd.DataFrame(value).sort_index(), len(symbols) - len(adj)


def _bear_order(hist: pd.DataFrame, weights: dict) -> tuple[pd.Series, int]:
    """Composite defensive score per ticker, best first (momentum-confirmed names ahead of the rest), plus the
    number of tickers without the full 70-row history."""
    p = hist.tail(BEAR_ROWS)
    scorable = p.columns[p.notna().all()] if len(p) >= BEAR_ROWS else p.columns[:0]
    p = p[scorable]
    last = p.iloc[-1]
    mom20, mom63 = last / p.iloc[-21] - 1, last / p.iloc[-64] - 1
    ret = p.pct_change().iloc[-20:]
    score = (
        weights["mom20"] * mom20 + weights["mom63"] * mom63 + weights["hit20"] * (ret > 0).mean()
        + weights["vol20"] * ret.std(ddof=0) + weights["dd63"] * (last / p.iloc[-64:].max() - 1)
    )
    frame = pd.DataFrame({"score": score, "tier": ((mom20 > 0) & (mom63 > 0)).map({True: 0, False: 1})})
    frame = frame.rename_axis("ticker").reset_index().sort_values(["tier", "score", "ticker"], ascending=[True, False, True])
    return frame.set_index("ticker").score, len(hist.columns) - len(scorable)


def select_bucket(adj: pd.DataFrame, value: pd.DataFrame, rebalance: str, strategy: dict, regime: str, sel: dict) -> tuple[list[dict], dict]:
    """Picks (best first) and exclusion counts for one bucket.

    A ticker needs a row on the rebalance date (no forward-fill), the full trend-MA and momentum history, and the
    liquidity minimum. Candidates have momentum > 0 and a price above the trend MA; plain momentum ranks them,
    except in BEAR where the composite score does and the candidate pool is not cut to top_n first.
    """
    counts = {"noRowOnRebalanceDate": 0, "insufficientHistory": 0, "illiquid": 0}
    top_n, lookback, ma, skip = strategy["top_n"], strategy["lookback"], strategy["stock_trend_ma"], sel["momentumSkipDays"]
    if top_n == 0 or adj.empty:
        return [], counts
    day = pd.Timestamp(rebalance)
    has_row = adj.loc[day].notna() if day in adj.index else pd.Series(False, index=adj.columns)
    counts["noRowOnRebalanceDate"] = int((~has_row).sum())
    hist = adj.loc[:, has_row]
    if len(hist) < max(ma + 5, lookback + skip + 2):
        counts["insufficientHistory"] = hist.shape[1]
        return [], counts

    trend = hist.iloc[-ma:].mean(skipna=False)  # NaN unless the whole window is present
    momentum = hist.iloc[-1 - skip] / hist.iloc[-1 - skip - lookback] - 1
    liq = sel["liquidity"]
    traded = value[hist.columns].tail(liq["windowDays"])
    adv = getattr(traded, liq["statistic"])() / CRORE
    complete = trend.notna() & momentum.notna() & (traded.notna().sum() >= liq["windowDays"])
    liquid = adv >= liq["minAdvCr"]
    counts["insufficientHistory"] = int((~complete).sum())
    counts["illiquid"] = int((complete & ~liquid).sum())

    price = hist.iloc[-1]
    ok = complete & liquid & (momentum > 0) & (price > trend)
    frame = pd.DataFrame({"momentum": momentum, "price": price, "trendMa": trend})[ok]
    if regime == "BEAR" and not frame.empty:
        order, unscored = _bear_order(hist[frame.index], sel["bearScore"])
        counts["insufficientHistory"] += unscored
        frame = frame.loc[order.index].assign(score=order)
    else:
        frame = frame.rename_axis("ticker").reset_index().sort_values(["momentum", "ticker"], ascending=[False, True]).set_index("ticker")
    picks = []
    for rank, (ticker, row) in enumerate(frame.head(top_n).iterrows(), start=1):
        pick = {"ticker": ticker, "rank": rank, **{k: round(float(row[k]), 6) for k in ("momentum", "price", "trendMa")}}
        if "score" in row:
            pick["score"] = round(float(row["score"]), 6)
        picks.append(pick)
    return picks, counts
