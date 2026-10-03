"""Offline data checks: the holiday file against the benchmark's dates, and the anomaly scan over the stored prices."""

from datetime import date

import numpy as np
import pandas as pd

from app.market.tradingcal import Calendar

ANOMALY_COLS = ["Ticker", "Date", "Kind", "Value", "Detail"]


def calendar_check(cal: Calendar, index_dates: list[str]) -> dict:
    """Compare the trading calendar with the dates the benchmark index actually traded.

    notTrading: index rows on days the calendar calls closed (a missing holiday-file entry or a missing special session).
    noIndexRow: calendar trading days without an index row (a bad holiday entry, or a gap in the index data).
    yearsWithoutHolidays: years in range with no holiday at all (the app refuses to run for such a year).
    """
    have = set(index_dates)
    start, end = date.fromisoformat(min(have)), date.fromisoformat(max(have))
    holiday_years = {h[:4] for h in cal.holidays}
    return {"first": min(have), "last": max(have), "indexRows": len(have),
            "notTrading": sorted(d for d in have if not cal.is_trading_day(d)),
            "noIndexRow": sorted(d.isoformat() for d in cal.days(start, end) if d.isoformat() not in have),
            "yearsWithoutHolidays": [str(y) for y in range(start.year, end.year + 1) if str(y) not in holiday_years]}


def _rows(ticker: str, dates, kind: str, values, detail: str) -> list[dict]:
    return [{"Ticker": ticker, "Date": d, "Kind": kind, "Value": round(float(v), 6), "Detail": detail} for d, v in zip(dates, values, strict=True)]


def scan(series: dict[str, pd.DataFrame], dividends: pd.DataFrame | None, prep: dict) -> pd.DataFrame:
    """Anomaly rows (Ticker, Date, Kind, Value, Detail) for the bhavcopy cross-check and for review.

    BIG_MOVE: |close-to-close return| above prep.bigMovePct. ZERO_VOLUME: a price bar with no volume.
    BAD_BAR: high below low, or open/close outside the high-low range. DIV_STEP: AdjClose/Close rose by more than
    prep.dividendStepMin on a day, which is how Yahoo books a dividend; the implied amount is compared with the
    Yahoo dividend table: DIV_UNLISTED (step, no dividend that day), DIV_NO_STEP (dividend, no step),
    DIV_AMOUNT (both, amounts differ by more than prep.dividendTolerance).
    """
    divs = {} if dividends is None else {t: dict(zip(g.ExDate, g.Amount.astype(float))) for t, g in dividends.groupby("Ticker")}
    out = []
    for t, df in series.items():
        d, close = df.Date.to_numpy(), df.Close.to_numpy(float)
        ret = np.r_[np.nan, close[1:] / close[:-1] - 1]
        for kind, mask in (("BIG_MOVE", np.abs(ret) > prep["bigMovePct"]), ("ZERO_VOLUME", df.Volume.to_numpy() == 0),
                           ("BAD_BAR", ((df.High < df.Low) | (df.Open > df.High) | (df.Open < df.Low) | (df.Close > df.High) | (df.Close < df.Low)).to_numpy())):
            out += _rows(t, d[mask], kind, ret[mask] if kind == "BIG_MOVE" else np.zeros(mask.sum()), "")
        f = df.AdjClose.to_numpy(float) / close
        rise = np.r_[np.nan, f[1:] / f[:-1] - 1]
        implied = np.r_[np.nan, close[:-1] * (1 - f[:-1] / f[1:])]  # Yahoo scales earlier AdjClose by 1 - D / previous close
        step = rise > prep["dividendStepMin"]
        listed = divs.get(t, {})
        out += _rows(t, d[step], "DIV_STEP", implied[step], "implied dividend per share")
        for day, val in zip(d[step], implied[step], strict=True):
            amount = listed.get(day)
            if amount is None:
                out += _rows(t, [day], "DIV_UNLISTED", [val], "step without a Yahoo dividend")
            elif abs(val - amount) > prep["dividendTolerance"] * amount:
                out += _rows(t, [day], "DIV_AMOUNT", [val], f"Yahoo dividend {amount}")
        have, stepped = set(d), set(d[step])
        for day, amount in listed.items():
            if day in have and day not in stepped:
                out += _rows(t, [day], "DIV_NO_STEP", [amount], "Yahoo dividend without an AdjClose step")
    return pd.DataFrame(out, columns=ANOMALY_COLS).sort_values(["Ticker", "Date", "Kind"], ignore_index=True)
