"""Point-in-time data: the stored price history loaded once (read-only), an as-of view that quacks like app.market.store.Store,
and the wide panels the target builder slices. Nothing here touches the network or writes under app/."""

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from app.market.common import COLS
from app.market.store import Store

INDEX_KEY = "NSEI"


def read_series(store: Store, key: str) -> pd.DataFrame:
    """Both storage tiers of one key, fresh winning a duplicate date, ascending by date. Empty frame when the key has no data."""
    df = pd.concat([store.read_archive(key), store.read_fresh(key)], ignore_index=True)
    df = df.dropna(subset=["Close"]).drop_duplicates("Date", keep="last").sort_values("Date")
    return df[COLS].reset_index(drop=True)


def read_universe(storage: Path, buckets: list[str]) -> dict[str, list[str]]:
    """bucket -> symbols of its newest storage file (today's membership; the v1 survivorship-biased universe)."""
    out = {}
    for b in buckets:
        files = sorted(storage.glob(f"{b}_*.csv"), key=lambda f: f.stem.rsplit("_", 1)[1])
        out[b] = sorted(set(pd.read_csv(files[-1], dtype=str, keep_default_na=False)["Symbol"]) - {""}) if files else []
    return out


class PitData:
    """All tickers' history in memory. `series` maps symbol -> frame (Date as ISO text, ascending); `index` is the benchmark."""

    def __init__(self, series: dict[str, pd.DataFrame], index: pd.DataFrame, buckets: dict[str, list[str]]):
        self.series, self.index, self.buckets = series, index, buckets
        self.membership = None  # set by add_pit: a point-in-time universe instead of the static buckets
        self._static = {b: set(s) for b, s in buckets.items()}
        self._reindex()

    def _reindex(self) -> None:
        self.dates = {k: df.Date.to_numpy() for k, df in self.series.items()}
        self.opens = {k: df.Open.to_numpy(float) for k, df in self.series.items()}
        self._panels = None

    def add_pit(self, extra: dict[str, pd.DataFrame], membership) -> None:
        """Switch to a point-in-time universe: add the derived series (Yahoo's win a clash) and use the membership for labels."""
        self.series = {**extra, **self.series}
        self.membership = membership
        self.buckets = {b: [] for b in membership.names}
        self._reindex()

    def members(self, asof: str) -> dict[str, set]:
        """bucket -> symbols as of a date: the static buckets, or the newest point-in-time membership on or before it."""
        return self._static if self.membership is None else self.membership.at(asof)

    @classmethod
    def load(cls, app_data: str | Path, bucket_names: list[str]) -> "PitData":
        market = Path(app_data) / "market"
        buckets = read_universe(Path(app_data) / "storage", bucket_names)
        eq, idx = Store(market, cutoff=""), Store(market / "indices", cutoff="")
        series = {s: df for b in buckets.values() for s in b if not (df := read_series(eq, s)).empty}
        return cls(series, read_series(idx, INDEX_KEY), buckets)

    def missing(self) -> list[str]:
        """Universe symbols with no stored history (not tradable; reported, never zero-filled)."""
        return sorted({s for b in self.buckets.values() for s in b} - set(self.series))

    def cut(self, key: str, asof: str) -> int:
        """Number of rows of `key` dated on or before asof."""
        return int(np.searchsorted(self.dates[key], asof, side="right"))

    def last_date(self, key: str) -> str | None:
        """Date of the last stored row of `key`, None for a ticker with no series."""
        d = self.dates.get(key)
        return str(d[-1]) if d is not None and len(d) else None

    def last_close(self, key: str) -> float:
        return float(self.series[key].Close.iloc[-1])

    def open_price(self, key: str, day: str) -> float | None:
        """Raw Open of `key` on `day`, None when the ticker has no row that day."""
        i = self.cut(key, day) if key in self.dates else 0
        return float(self.opens[key][i - 1]) if i and self.dates[key][i - 1] == day else None

    def bench_close(self, asof: str) -> float | None:
        row = self.index[self.index.Date == asof]
        return float(row.Close.iloc[0]) if len(row) else None

    def panels(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Date-by-ticker AdjClose and traded value (raw Close x Volume) over the whole history, built once."""
        if self._panels is None:
            idx = lambda df: pd.DatetimeIndex(df.Date)  # noqa: E731
            adj = pd.DataFrame({k: pd.Series(df.AdjClose.to_numpy(float), index=idx(df)) for k, df in self.series.items()}).sort_index()
            value = pd.DataFrame({k: pd.Series((df.Close * df.Volume).to_numpy(float), index=idx(df)) for k, df in self.series.items()}).sort_index()
            self._panels = (adj, value)
        return self._panels

    def data_hash(self) -> str:
        """Content hash of everything the run reads (feeds the trial registry and the determinism test)."""
        h = hashlib.sha256()
        if self.membership is not None:
            h.update(self.membership.hash_bytes())
        for key, df in sorted({**self.series, "^" + INDEX_KEY: self.index}.items()):
            h.update(key.encode())
            h.update(pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes())
        return h.hexdigest()


class PitStore:
    """Drop-in for the Store that app.risk.common.history reads: rows after `asof` do not exist for the caller."""

    def __init__(self, data: PitData):
        self.data, self.asof = data, None

    def read_fresh(self, key: str) -> pd.DataFrame:
        if self.asof is None:
            raise RuntimeError("PitStore.asof is not set")
        df = self.data.series.get(key)
        return pd.DataFrame(columns=COLS) if df is None else df.iloc[:self.data.cut(key, self.asof)]

    def read_archive(self, key: str) -> pd.DataFrame:
        return pd.DataFrame(columns=COLS)
