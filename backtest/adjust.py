"""Corporate-action adjustment of bhavcopy-only names, from NSE's own reference price.

On an ex-date NSE sets that day's PREVCLOSE to the previous close adjusted for the action (split, bonus, dividend, rights). So
f = PrevClose / last row's Close is the action's price factor, with no external corporate-action file. Each event is classified:

  quiet       |f - 1| within quietTolerance: nothing happened (rounding).
  split-like  1/f or f is a usual split/bonus multiplier: the price AND volume history before it is rescaled
              (Close, Open, High, Low divided by the multiplier, Volume multiplied), like Yahoo's split-adjusted Close.
  cash-like   0.80 < f < 1 and not split-like (dividend, rights): only AdjClose carries it, like Yahoo's dividend adjustment.
  unresolved  anything else (a jump up that is no clean consolidation, a fall of 20% or more that is no split): a break.

Unresolved breaks are not guessed at: the series is cut there and only the part after the LAST break is kept, so the name is not
tradable across it. Every cut is returned for review.
"""

import numpy as np
import pandas as pd

NICE = (1.1, 1.2, 1.25, 4 / 3, 1.5, 5 / 3, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 50, 100)
COLS = ["Ticker", "Date", "Open", "High", "Low", "Close", "AdjClose", "Volume"]
DEFAULTS = {"quietTolerance": 0.003, "niceTolerance": 0.02, "cashMin": 0.80}


def nice(m: float, tol: float) -> bool:
    return any(abs(m / n - 1) < tol for n in NICE)


def classify(f: float, tol: dict) -> str:
    if abs(f - 1) <= tol["quietTolerance"]:
        return "quiet"
    if f < 1 and nice(1 / f, tol["niceTolerance"]):
        return "split"
    if f > 1 and nice(f, tol["niceTolerance"]):
        return "reverse"  # a consolidation: the multiplier is below 1
    if tol["cashMin"] < f < 1:
        return "cash"
    return "unresolved"


def events(df: pd.DataFrame, tol: dict) -> pd.DataFrame:
    """Date, f, kind of every row whose PrevClose differs from the previous row's Close (df sorted by Date with Close, PrevClose)."""
    close, prev = df.Close.to_numpy(float), df.PrevClose.to_numpy(float)
    f = np.ones(len(df))
    ok = (np.arange(len(df)) > 0) & (prev > 0) & np.isfinite(prev)
    f[1:] = np.where(ok[1:], prev[1:] / close[:-1], 1.0)
    rows = [(df.Date.iloc[i], float(f[i]), classify(f[i], tol)) for i in range(1, len(df)) if classify(f[i], tol) != "quiet"]
    return pd.DataFrame(rows, columns=["Date", "f", "kind"])


def adjust_security(raw: pd.DataFrame, ticker: str, tol: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """(frame in the stored COLS layout, report) for one security's raw rows (Date, Open, High, Low, Close, PrevClose, Volume)."""
    tol = {**DEFAULTS, **(tol or {})}
    df = raw.drop_duplicates("Date").sort_values("Date").dropna(subset=["Close"]).reset_index(drop=True)
    df = df[df.Close > 0].reset_index(drop=True)
    ev = events(df, tol)
    cuts = list(ev[ev.kind == "unresolved"].Date)
    if cuts:
        df = df[df.Date >= cuts[-1]].reset_index(drop=True)
        ev = events(df, tol)
    n = len(df)
    split_f, all_f = np.ones(n), np.ones(n)
    idx = {d: i for i, d in enumerate(df.Date)}
    for r in ev.itertuples():
        i = idx[r.Date]
        all_f[:i] *= r.f
        if r.kind in ("split", "reverse"):
            split_f[:i] *= r.f
    out = pd.DataFrame({
        "Ticker": ticker, "Date": df.Date.to_numpy(),
        "Open": df.Open.to_numpy(float) * split_f, "High": df.High.to_numpy(float) * split_f, "Low": df.Low.to_numpy(float) * split_f,
        "Close": df.Close.to_numpy(float) * split_f, "AdjClose": df.Close.to_numpy(float) * all_f,
        "Volume": np.rint(df.Volume.to_numpy(float) / split_f).astype("int64")})
    return out[COLS], {"events": ev, "cutAt": cuts, "rows": n}
