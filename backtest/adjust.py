"""Corporate-action adjustment of bhavcopy-only names, from the price and volume series themselves.

NSE's bhavcopy PREVCLOSE is NOT adjusted on an ex-date (checked against Yahoo's split table on real data: 0 of 171 splits found
that way), so a split or bonus has to be recognised from what it does to the tape: the price jumps by a usual factor k AND the
volume level shifts by the same factor and stays shifted. A crash or a squeeze moves the price just as far but its volume spikes
and decays. Per day with a price move beyond minMove (30%, i.e. a factor over 1.43):

  split       price fell by a usual factor k (1.5, 5/3, 1.75, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 50, 100, +-niceTolerance) AND either the
              median volume of the next volumeWindow sessions is about k times the previous window's (+-volumeTolerance), or the
              price landed within tightTolerance of the factor and the day does not look like a crash (a one-day volume spike
              over crashSpike x the normal level that does not last). Real volumes after a split are erratic (checked against
              Yahoo's split table on real data), but a genuine crash seldom lands within 3% of an exact 2x or 5x. Earlier prices
              are divided by k, earlier volumes multiplied by k, like Yahoo's split-adjusted Close and Volume.
  reverse     the mirror image: price rose by k, volume fell to about 1/k.
  move        the volume level did not change (ratio between 0.5 and 2): a genuine crash or surge. Kept as it is; cutting at real
              crashes would delete the very failures a survivorship-free universe needs.
  unresolved  anything else (big move, volume shifted, no usual factor): the series is cut there; only the part after the LAST
              such break is kept, so the name is not tradable across it. Every cut is returned for review.
  pending     fewer than 5 sessions after the move to judge by (the end of the data): left alone.

Moves under 30% (dividends, rights issues, ordinary days) are not adjusted, so AdjClose equals the split-adjusted Close here: a
dead name's total return is understated by its dividends. Mergers are not adjustments: the swap ratio is not a price factor.
"""

import numpy as np
import pandas as pd

NICE = (1.5, 5 / 3, 1.75, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 50, 100)
COLS = ["Ticker", "Date", "Open", "High", "Low", "Close", "AdjClose", "Volume"]
DEFAULTS = {"minMove": 0.30, "niceTolerance": 0.05, "volumeWindow": 10, "volumeTolerance": 0.40, "minPost": 5, "tightTolerance": 0.03, "crashSpike": 4.0}


def nearest_nice(k: float, tol: float) -> float | None:
    best = min(NICE, key=lambda n: abs(k / n - 1))
    return best if abs(k / best - 1) <= tol else None


def events(df: pd.DataFrame, tol: dict) -> pd.DataFrame:
    """Date, r (close / previous close), k (usual factor or NaN), vr (volume ratio after / before), kind for every large move."""
    close, vol = df.Close.to_numpy(float), df.Volume.to_numpy(float)
    w, out = tol["volumeWindow"], []
    floor = 1 / (1 - tol["minMove"])
    for i in range(1, len(df)):
        r = close[i] / close[i - 1]
        k = 1 / r if r < 1 else r
        if k <= floor:
            continue
        pre, post = vol[max(0, i - w):i], vol[i:i + w]
        if len(post) < tol["minPost"] or len(pre) < tol["minPost"] or np.median(pre) <= 0:
            out.append((df.Date.iloc[i], r, np.nan, np.nan, "pending"))
            continue
        pre_med = float(np.median(pre))
        vr = float(np.median(post) / pre_med)
        n = nearest_nice(k, tol["niceTolerance"])
        tight = bool(n) and tol["tightTolerance"] > 0 and abs(k / n - 1) <= tol["tightTolerance"]  # 0 turns the price-only rule off
        crash = vol[i] > tol["crashSpike"] * pre_med and vr < 1.5  # a one-day volume spike that does not stay: news, not a share-count change
        vol_ok = bool(n) and (abs(vr / n - 1) <= tol["volumeTolerance"] if r < 1 else abs(vr * n - 1) <= tol["volumeTolerance"])
        if n and (vol_ok or (tight and not crash)):
            kind = "split" if r < 1 else "reverse"
        elif 0.5 <= vr <= 2.0:
            kind = "move"
        else:
            kind = "unresolved"
        out.append((df.Date.iloc[i], r, n if n else np.nan, vr, kind))
    return pd.DataFrame(out, columns=["Date", "r", "k", "vr", "kind"])


def adjust_security(raw: pd.DataFrame, ticker: str, tol: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """(frame in the stored COLS layout, report) for one security's raw rows (Date, Open, High, Low, Close, Volume)."""
    tol = {**DEFAULTS, **(tol or {})}
    df = raw.drop_duplicates("Date").sort_values("Date").dropna(subset=["Close"])
    df = df[df.Close > 0].reset_index(drop=True)
    ev = events(df, tol)
    cuts = list(ev[ev.kind == "unresolved"].Date)
    if cuts:
        df = df[df.Date >= cuts[-1]].reset_index(drop=True)
        ev = events(df, tol)
    factor = np.ones(len(df))  # multiply an earlier row's price by this to put it on the latest share basis
    idx = {d: i for i, d in enumerate(df.Date)}
    for r in ev.itertuples():
        if r.kind == "split":
            factor[:idx[r.Date]] /= r.k
        elif r.kind == "reverse":
            factor[:idx[r.Date]] *= r.k
    price = lambda col: df[col].to_numpy(float) * factor  # noqa: E731
    out = pd.DataFrame({"Ticker": ticker, "Date": df.Date.to_numpy(), "Open": price("Open"), "High": price("High"), "Low": price("Low"),
                        "Close": price("Close"), "AdjClose": price("Close"), "Volume": np.rint(df.Volume.to_numpy(float) / factor).astype("int64")})
    return out[COLS], {"events": ev, "cutAt": cuts, "rows": len(df)}
