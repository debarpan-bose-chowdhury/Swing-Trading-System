"""Fetch -> validate -> commit steps shared by Migrator and Updator."""

import json
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from app.market.common import Report, iso, shift
from app.market.fetcher import Fetcher
from app.market.store import Store
from app.market.tradingcal import Calendar
from app.market.validator import NULL_VALUE, validate


class Series(NamedTuple):
    ticker: str  # stored in the Ticker column: NSE symbol, or the Yahoo index symbol
    yahoo: str
    key: str  # file name stem
    store: Store

    @property
    def is_index(self) -> bool:
        return self.ticker.startswith("^")


def build_series(cfg: dict, symbols: list[str], cutoff: str) -> list[Series]:
    """Equities from registry symbols plus the configured indices, each with its own store."""
    market = Path(cfg["paths"]["market"])
    comp = cfg["archiver"]["parquet"]["compression"]
    eq, idx = Store(market, cutoff, comp), Store(market / "indices", cutoff, comp)
    indices = json.loads(Path(cfg["paths"]["indices"]).read_text(encoding="utf-8"))["indices"]
    return [Series(s, f"{s}.NS", s, eq) for s in symbols] + [Series(i, i, i.lstrip("^"), idx) for i in indices]


def _clip(frame: pd.DataFrame, yahoo: str, ticker: str, last: str, first: str = "") -> pd.DataFrame:
    """One ticker's rows from `first` (the history floor) up to the last final session (no partial bars),
    Ticker renamed to the stored form."""
    rows = frame[(frame.Ticker == yahoo) & (frame.Date >= first) & (frame.Date <= last)]
    return rows.assign(Ticker=ticker)


def _validated(fx: Fetcher, cal: Calendar, cfg: dict, s: Series, raw: pd.DataFrame, last: str):
    """Validate one ticker's rows; refetch NULL_VALUE rows (bounded passes), replacing them by key."""
    valid, rej = validate(raw, cal)
    for _ in range(cfg["fetch"]["nullRefetchPasses"]):
        nulls = rej[rej.Reason == NULL_VALUE]
        if nulls.empty:
            break
        keys = set(nulls.Date)
        frame = fx.fetch([s.yahoo], start=min(keys), end=shift(max(keys), 1))
        if frame is None:
            break
        new = _clip(frame, s.yahoo, s.ticker, last, cfg["fetch"].get("historyStart", ""))
        new = new[new.Date.isin(keys)]
        v2, r2 = validate(new, cal)
        valid = pd.concat([valid, v2], ignore_index=True)
        rej = pd.concat([rej[rej.Reason != NULL_VALUE], nulls[~nulls.Date.isin(new.Date)], r2], ignore_index=True)
    return valid, rej


def fetch_valid(fx: Fetcher, cal: Calendar, cfg: dict, report: Report, series: list[Series], last: str, **kw):
    """Yield (series, valid, rejects, raw) per fetched series; equities batched, indices one at a time.

    last is the last final session (ISO); later rows are dropped so no partial bar is ever stored.

    Failed or deferred series go to report.failed; rejected rows go to report.rejects.
    """
    groups = ((cfg["fetch"]["batchSize"], [s for s in series if not s.is_index]), (1, [s for s in series if s.is_index]))
    for size, group in groups:
        by_yahoo = {s.yahoo: s for s in group}
        for chunk, frame in fx.batches(list(by_yahoo), size, **kw):
            for y in chunk:
                s = by_yahoo[y]
                if frame is None:
                    report.failed[s.ticker] = "deferred: rate-limit pauses used up" if fx.exhausted else "fetch failed after retries"
                    continue
                raw = _clip(frame, y, s.ticker, last, cfg["fetch"].get("historyStart", ""))
                valid, rej = _validated(fx, cal, cfg, s, raw, last)
                if len(rej):
                    report.rejects.append(rej)
                yield s, valid, rej, raw


def too_many_rejects(cfg: dict, rejected: int, returned: int) -> bool:
    return returned > 0 and rejected / returned > cfg["validator"]["rejectShareRollback"]


def backfill(fx, cal, cfg, report, series, last, on_done=None) -> dict[str, int]:
    """Max-history fetch, validate, atomic per-ticker rebuild. Returns rows returned per ticker.

    on_done(series) fires once a ticker is settled: committed, or fetched successfully but empty.
    """
    got: dict[str, int] = {}
    for s, valid, rej, raw in fetch_valid(fx, cal, cfg, report, series, last, period="max"):
        got[s.ticker] = len(raw)
        if too_many_rejects(cfg, len(rej), len(raw)):
            report.failed[s.ticker] = f"rolled back: {len(rej)}/{len(raw)} rows rejected"
            continue
        try:
            if not valid.empty:
                s.store.rebuild(s.key, valid)
                report.updated.add(s.ticker)
        except Exception as e:
            report.failed[s.ticker] = f"write failed: {e!r}"
            continue
        if on_done:
            on_done(s)
    return got
