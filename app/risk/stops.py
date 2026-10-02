"""Stop engine: ATR20 on adjusted prices and the ratcheting trailing stop, replayed from the track start every run."""

import numpy as np
import pandas as pd


def adjusted(df: pd.DataFrame) -> pd.DataFrame:
    """Date, adjusted High / Low (H, L) and AdjClose (C): each bar scaled by f = AdjClose / Close."""
    f = df.AdjClose / df.Close
    return pd.DataFrame({"Date": df.Date.to_numpy(), "H": (df.High * f).to_numpy(), "L": (df.Low * f).to_numpy(), "C": df.AdjClose.to_numpy()})


def atr(a: pd.DataFrame, period: int) -> pd.Series:
    """Simple mean of the last `period` true ranges; NaN until period + 1 rows exist."""
    prev = a.C.shift().fillna(a.C)  # the first row has no previous close: its true range is dropped below
    tr = np.maximum(np.maximum(a.H - a.L, (a.H - prev).abs()), (a.L - prev).abs())
    tr.iloc[:1] = np.nan
    return tr.rolling(period).mean()


def width_pct(atr_value: float, close: float, k: float, lo: float, hi: float) -> float:
    """Nominal stop width as a fraction of price: clamp(k x ATR%, lo, hi); the widest clamp when ATR is unknown."""
    return hi if not np.isfinite(atr_value) else min(max(k * atr_value / close, lo), hi)


def stop_path(df: pd.DataFrame, start: str, k: float, lo: float, hi: float, period: int) -> pd.DataFrame:
    """Date, C, hwm, atr and stop for every bar from `start` (a bar before the first row starts at the first row).

    width = max(lo x hwm, min(k x ATR, hi x hwm)); stop = ratchet of hwm - width. Without ATR the width is hi x hwm.
    """
    a = adjusted(df)
    a["atr"] = atr(a, period)
    a = a[a.Date >= start].reset_index(drop=True)
    a["hwm"] = a.C.cummax()
    width = np.maximum(lo * a.hwm, np.minimum(k * a.atr.fillna(np.inf), hi * a.hwm))
    a["stop"] = (a.hwm - width).cummax()
    return a


def first_breach(path: pd.DataFrame, after: str | None, asof: str) -> dict | None:
    """First bar in (after, asof] closing at or below its stop; the first bar of the path can never trigger."""
    bars = path.iloc[1:]
    hit = bars[(bars.stop >= bars.C) & (bars.Date <= asof) & ((bars.Date > after) if after else (bars.Date == asof))]
    if hit.empty:
        return None
    row = hit.iloc[0]
    return {"breachDate": row.Date, "stopPrice": float(row.stop)}
