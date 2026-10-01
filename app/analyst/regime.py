"""Market regime from the index close: daily raw regime, weekly rebalance dates, persistence (active/pending)."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.market.store import Store
from app.market.tradingcal import Calendar

UNKNOWN_ROWS = 209  # the original rule: with fewer than 210 observations the regime is Unknown (kept as is)
HISTORY_COLS = ["date", "raw_regime", "active_regime", "pending_regime", "pending_remaining_days"]


def index_close(cfg: dict) -> pd.Series:
    """Close of regime.index from both storage tiers (fresh wins on a duplicate date), ascending by date."""
    key = cfg["regime"]["index"].lstrip("^")
    store = Store(Path(cfg["paths"]["market"]) / "indices", cutoff="")
    df = pd.concat([store.read_archive(key), store.read_fresh(key)], ignore_index=True)
    df = df.drop_duplicates("Date", keep="last").dropna(subset=["Close"]).sort_values("Date")
    if len(df) < cfg["regime"]["minRows"]:
        raise ValueError(f"index {cfg['regime']['index']} has {len(df)} rows, need at least {cfg['regime']['minRows']}")
    return pd.Series(df.Close.to_numpy(dtype=float), index=pd.to_datetime(df.Date))


def raw_regimes(close: pd.Series) -> pd.Series:
    """BULL / TREND / WEAK / BEAR for every date (Unknown for the first 209 observations)."""
    above200 = close > close.rolling(200).mean()
    above50 = close > close.rolling(50).mean()
    up63 = close.pct_change(63) > 0
    raw = np.select([above200 & above50 & up63, above200, above50], ["BULL", "TREND", "WEAK"], "BEAR").astype(object)
    raw[:UNKNOWN_ROWS] = "Unknown"
    return pd.Series(raw, index=close.index)


def rebalance_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last Monday-Friday date present in each ISO week (weekend special sessions are ignored)."""
    days = index[index.weekday < 5]
    iso = days.isocalendar()
    return pd.DatetimeIndex(pd.Series(days, index=days).groupby([iso.year.to_numpy(), iso.week.to_numpy()]).max().sort_values())


def live_rebalance_date(cal: Calendar, today: date) -> date | None:
    """Last Monday-Friday NSE trading day of today's ISO week, None when the week has none."""
    monday = today - timedelta(days=today.weekday())
    week = [monday + timedelta(days=n) for n in range(5)]
    return next((d for d in reversed(week) if cal.is_trading_day(d)), None)


def weekly_state(raw: pd.Series, persistence: int) -> pd.DataFrame:
    """Active / pending regime per rebalance date from the weekly raw regimes.

    BEAR activates at once; any other regime activates on its `persistence`-th consecutive week. The raw regime
    is always the pending one; the countdown is zero while it equals the active regime.
    """
    active, prev, run, rows = "Unknown", None, 0, []
    for day, r in raw.items():
        run = run + 1 if r == prev else 1
        prev = r
        if r == "BEAR" or run >= persistence:
            active = r
        left = 0 if r == active else max(0, persistence - run) * 7
        rows.append((day.date().isoformat(), r, active, r, left))
    return pd.DataFrame(rows, columns=HISTORY_COLS)


def regime_history(close: pd.Series, persistence: int) -> pd.DataFrame:
    raw = raw_regimes(close)
    return weekly_state(raw[rebalance_dates(close.index)], persistence)
