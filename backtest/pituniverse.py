"""Point-in-time universe: who is Large / Mid / Small on each rebalance date, and where the prices of the names come from.

Ranking (bhavcopy only, so dead names are in it): on a rebalance date every name is ranked by its median traded value over the last
`rankWindow` sessions (at least `rankMinObs` of them present); the top 50 are LargeCap, the next 50 MidCap, the next 50 SmallCap
(sizes from app/config/config.json). It is a liquidity proxy for the live market-cap rule. A renamed company is one name (links.py).
Only data up to the date is used, so a name's label on day t cannot depend on what happened later.

Prices: the stored Yahoo series wherever Yahoo has the name (today's names); otherwise the bhavcopy rows adjusted by adjust.py.
Only names that were in the top `scopeTop` at some month end are priced (scope), which keeps the corporate-action review small.
A name whose derived series is unusable is skipped when ranking, so the bands stay full, and the skipped slots are counted as
"holes" in the report: that count is the survivorship bias still left in.
"""

import json
from bisect import bisect_right
from pathlib import Path

import numpy as np
import pandas as pd

from app.analyst.regime import rebalance_dates
from backtest import adjust, bhav, links

PIT_DIR = "pit"
SERIES_FILE, MEMBER_FILE, REPORT_FILE = "series.parquet", "membership.parquet", "report.json"


def band_sizes(app_config: Path) -> dict[str, int]:
    """bucket -> how many names it holds, highest tier first (the live capBuckets topN)."""
    return {b["name"]: int(b["topN"]) for b in json.loads((app_config / "config.json").read_text(encoding="utf-8"))["filter"]["capBuckets"]}


def value_panel(rows: pd.DataFrame, final: dict[str, str]) -> pd.DataFrame:
    """Date-by-symbol traded value, renamed symbols merged into their final symbol (the larger value when both trade on a day)."""
    r = rows.assign(Ticker=rows.Ticker.map(lambda t: final.get(t, t)))
    return r.groupby(["Date", "Ticker"]).Value.max().unstack().sort_index().astype("float32")


def trailing_median(panel: pd.DataFrame, dates: list[str], window: int, min_obs: int) -> pd.DataFrame:
    """Median traded value over the last `window` panel rows ending at each date (NaN unless min_obs are present). Rows = dates."""
    pos = panel.index.searchsorted(dates, side="right")
    out = np.full((len(dates), panel.shape[1]), np.nan, dtype="float32")
    v = panel.to_numpy()
    for k, n in enumerate(pos):
        w = v[max(0, n - window):n]
        enough = np.isfinite(w).sum(0) >= min_obs
        if enough.any():
            out[k, enough] = np.nanmedian(w[:, enough], axis=0)
    return pd.DataFrame(out, index=dates, columns=panel.columns)


def month_ends(dates: list[str]) -> list[str]:
    s = pd.Series(dates, index=pd.DatetimeIndex(dates))
    return list(s.groupby([s.index.year, s.index.month]).max())


def scope_symbols(panel: pd.DataFrame, top: int, window: int, min_obs: int, exclude: str | None) -> list[str]:
    """Symbols that were in the top `top` by trailing median traded value at any month end."""
    med = trailing_median(panel, month_ends(list(panel.index)), window, min_obs)
    if exclude:
        med = med.loc[:, ~med.columns.str.contains(exclude, regex=True)]
    keep: set[str] = set()
    for _, row in med.iterrows():
        keep |= set(row.dropna().nlargest(top).index)
    return sorted(keep)


def usable(series: pd.DataFrame, day: str, gap: int = 5) -> bool:
    """The name has a price row on `day` or within the previous `gap` calendar days (a halted day is not a dead name)."""
    i = int(np.searchsorted(series.Date.to_numpy(), day, side="right"))
    return bool(i) and (pd.Timestamp(day) - pd.Timestamp(series.Date.iloc[i - 1])).days <= gap


class Membership:
    """bucket -> symbols for each rebalance date; at(asof) is the newest membership on or before asof."""

    def __init__(self, table: pd.DataFrame, names: list[str]):
        self.names, self.table = names, table
        self.days, self._sets = [], []
        for day, g in table.groupby("Date"):
            self.days.append(day)
            self._sets.append({b: set(g[g.Bucket == b].Symbol) for b in names})
        self.empty = {b: set() for b in names}

    def at(self, asof: str) -> dict[str, set]:
        i = bisect_right(self.days, asof)
        return self._sets[i - 1] if i else self.empty

    def hash_bytes(self) -> bytes:
        return pd.util.hash_pandas_object(self.table.sort_values(["Date", "Bucket", "Symbol"]), index=False).to_numpy().tobytes()


def memberships(med: pd.DataFrame, has_series, sizes: dict[str, int], exclude: str | None) -> tuple[pd.DataFrame, dict]:
    """(membership rows Date, Bucket, Symbol, Rank; per-date holes) from the trailing medians at the rebalance dates.

    has_series(symbol, date) says whether a usable price series exists. Unusable names are skipped; `holes` counts how many of the
    unrestricted top-N slots they would have taken.
    """
    total, rows, holes = sum(sizes.values()), [], {}
    cols = [c for c in med.columns if not (exclude and pd.Series([c]).str.contains(exclude, regex=True).iloc[0])]
    for day, row in med[cols].iterrows():
        ranked = row.dropna().sort_values(ascending=False)
        top = list(ranked.index[:total])
        good = [s for s in ranked.index if has_series(s, day)][:total]
        holes[day] = len(set(top) - set(good))
        start = 0
        for b, n in sizes.items():
            for rank, s in enumerate(good[start:start + n], start + 1):
                rows.append((day, b, s, rank))
            start += n
    return pd.DataFrame(rows, columns=["Date", "Bucket", "Symbol", "Rank"]), holes


def build(cfg: dict, yahoo_series: dict[str, pd.DataFrame], index_dates: list[str]) -> dict:
    """Rank, price and label. Writes backtest/data/pit/{series,membership}.parquet and report.json; returns the report."""
    u = cfg["universe"]
    out = Path(cfg["paths"]["data"]) / PIT_DIR
    out.mkdir(parents=True, exist_ok=True)
    rows = bhav.load(cfg)
    lk_path = Path(cfg["paths"]["data"]) / "symbol_links.csv"
    table = pd.read_csv(lk_path, dtype=str, keep_default_na=False) if lk_path.exists() else pd.DataFrame(columns=links.LINK_COLS)
    final = links.resolve(table)
    panel = value_panel(rows[["Ticker", "Date", "Value"]], final)
    scope = scope_symbols(panel, u["scopeTop"], u["rankWindow"], u["rankMinObs"], u["excludePattern"])

    derived, cuts, kept = {}, [], 0
    preds: dict[str, list[str]] = {}
    for old, new in final.items():
        preds.setdefault(new, []).append(old)
    need = [s for s in scope if s not in yahoo_series]
    sub = rows[rows.Ticker.isin(set(need) | {o for s in need for o in preds.get(s, [])})]
    sub = sub.assign(Final=sub.Ticker.map(lambda t: final.get(t, t)))
    for sym, g in sub.groupby("Final"):
        g = g.sort_values(["Date", "Ticker"]).drop_duplicates("Date", keep="last")
        series, rep = adjust.adjust_security(g[["Date", "Open", "High", "Low", "Close", "Volume"]], sym, u["adjust"])
        if len(series) >= 30:
            derived[sym] = series
            kept += 1
        if rep["cutAt"]:
            cuts.append((sym, rep["cutAt"][-1], len(g), len(series)))
    allseries = {**{s: yahoo_series[s] for s in scope if s in yahoo_series}, **derived}

    dates = [str(d.date()) for d in rebalance_dates(pd.DatetimeIndex(index_dates))]
    med = trailing_median(panel[[c for c in scope if c in panel.columns]], dates, u["rankWindow"], u["rankMinObs"])
    sizes = band_sizes(Path(cfg["paths"]["appConfig"]))
    mem, holes = memberships(med, lambda s, d: s in allseries and usable(allseries[s], d), sizes, u["excludePattern"])

    pd.concat(derived.values(), ignore_index=True).to_parquet(out / SERIES_FILE, index=False) if derived else None
    mem.to_parquet(out / MEMBER_FILE, index=False)
    h = pd.Series(holes)
    report = {"scope": len(scope), "fromYahoo": len([s for s in scope if s in yahoo_series]), "derived": len(derived), "scopeWithoutUsableSeries": len(set(scope) - set(allseries)),
              "cuts": len(cuts), "cutExamples": cuts[:15], "rebalanceDates": len(dates), "membershipRows": len(mem),
              "holesPerDate": {"mean": round(float(h.mean()), 1), "max": int(h.max()), "byYear": {y: round(float(g.mean()), 1) for y, g in h.groupby(h.index.str[:4])}}}
    (out / REPORT_FILE).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def attach(data, cfg: dict) -> None:
    """Add the derived series and the membership to a PitData loaded from Yahoo's files (mode "pit")."""
    from backtest.prep import MissingInput
    d = Path(cfg["paths"]["data"]) / PIT_DIR
    if not (d / MEMBER_FILE).exists():
        raise MissingInput("no point-in-time universe: run `python -m backtest.universe --build-pit` first")
    mem = Membership(pd.read_parquet(d / MEMBER_FILE), list(band_sizes(Path(cfg["paths"]["appConfig"]))))
    extra = {}
    if (d / SERIES_FILE).exists():
        s = pd.read_parquet(d / SERIES_FILE)
        extra = {t: g.reset_index(drop=True) for t, g in s.groupby("Ticker")}
    data.add_pit(extra, mem)
