"""Row-level validation shared by every stage. Rejected rows come back with a Reason code."""

import pandas as pd

from app.market.common import COLS
from app.market.tradingcal import Calendar

NUMERIC = COLS[2:]
NULL_VALUE = "NULL_VALUE"


def validate(df: pd.DataFrame, cal: Calendar) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (valid, rejects). First failing rule wins: SCHEMA, NULL_VALUE, OHLC, DUP_IN_BATCH, NON_TRADING_DAY.

    NULL_VALUE rows are the caller's to refetch; those still null after the refetch passes stay rejected.
    """
    df = df.reset_index(drop=True)
    if df.empty:
        return df, df.assign(Reason=pd.Series(dtype=object))
    reason = pd.Series(None, index=df.index, dtype=object)

    def flag(code: str, mask: pd.Series) -> None:
        reason[reason.isna() & mask] = code

    if not set(COLS) <= set(df.columns):
        flag("SCHEMA", pd.Series(True, index=df.index))
        return df[reason.isna()], df[reason.notna()].assign(Reason=reason.dropna())

    num = df[NUMERIC].apply(pd.to_numeric, errors="coerce")
    day = pd.to_datetime(df.Date, format="%Y-%m-%d", errors="coerce")
    ticker_ok = df.Ticker.notna() & (df.Ticker.astype(str).str.strip() != "")
    bad_num = (num.isna() & df[NUMERIC].notna()).any(axis=1)  # present but not numeric
    flag("SCHEMA", ~ticker_ok | day.isna() | bad_num | (num.Volume % 1 != 0))
    flag(NULL_VALUE, num.isna().any(axis=1))
    flag(
        "OHLC",
        (num.High < num[["Open", "Close", "Low"]].max(axis=1)) | (num.Low > num[["Open", "Close", "High"]].min(axis=1)) | (num.Volume < 0),
    )
    flag("DUP_IN_BATCH", df.duplicated(["Ticker", "Date"], keep=False))
    flag("NON_TRADING_DAY", ~day.dt.date.map(lambda d: cal.is_trading_day(d) if pd.notna(d) else True))

    ok = reason.isna()
    valid = df[ok].copy()
    valid[NUMERIC] = num[ok]
    valid["Volume"] = valid.Volume.astype("int64")
    return valid, df[~ok].assign(Reason=reason[~ok])
