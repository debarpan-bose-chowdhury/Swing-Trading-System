"""Two-tier per-ticker storage: fresh CSV and archive Parquet+zstd. Dates are ISO strings in memory."""

import shutil
from pathlib import Path

import pandas as pd

from app.market.common import COLS, write_csv, atomic

KEYS = ["Ticker", "Date"]


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLS].drop_duplicates(KEYS, keep="last").sort_values("Date", ignore_index=True)
    return df.astype({"Ticker": str, "Date": str, "Volume": "int64"})


def write_parquet(df: pd.DataFrame, path: Path, compression: str) -> None:
    atomic(path, lambda tmp: df.assign(Date=pd.to_datetime(df.Date).dt.date).to_parquet(tmp, compression=compression, index=False))


def partition_name(df: pd.DataFrame) -> str:
    return f"Compressed_{df.Date.min()}_{df.Date.max()}.parquet"


class Store:
    """root/fresh/{key}.csv and root/archive/{key}/Compressed_{first}_{last}.parquet; cutoff is an ISO date."""

    def __init__(self, root: str | Path, cutoff: str, compression: str = "zstd"):
        self.root, self.cutoff, self.compression = Path(root), cutoff, compression

    def fresh(self, key: str) -> Path:
        return self.root / "fresh" / f"{key}.csv"

    def archive(self, key: str) -> Path:
        return self.root / "archive" / key

    def read_fresh(self, key: str) -> pd.DataFrame:
        if not self.fresh(key).exists():
            return pd.DataFrame(columns=COLS)
        return pd.read_csv(self.fresh(key), dtype={"Ticker": str, "Date": str}, keep_default_na=False, na_values=[""])

    def read_archive(self, key: str) -> pd.DataFrame:
        files = sorted(self.archive(key).glob("*.parquet"))
        if not files:
            return pd.DataFrame(columns=COLS)
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        return df.assign(Date=df.Date.astype(str))

    def last_date(self, key: str) -> str | None:
        """Newest stored date across both tiers, None if the ticker has no history."""
        df = self.read_fresh(key)
        if df.empty:
            df = self.read_archive(key)
        return None if df.empty else df.Date.max()

    def upsert(self, key: str, rows: pd.DataFrame) -> None:
        """Merge rows into the fresh CSV on (Ticker, Date); incoming rows win."""
        write_csv(clean(pd.concat([self.read_fresh(key), rows[COLS]], ignore_index=True)), self.fresh(key))

    def rebuild(self, key: str, rows: pd.DataFrame) -> None:
        """Replace a ticker's whole history: rows before the cutoff -> one Parquet partition, the rest -> CSV.

        Both are built in a temp folder first, then swapped in (archive first, so a crash can only duplicate).
        """
        df = clean(rows)
        old, new = df[df.Date < self.cutoff], df[df.Date >= self.cutoff]
        tmp = self.root / ".tmp" / key
        shutil.rmtree(tmp, ignore_errors=True)
        if not old.empty:
            write_parquet(old, tmp / "archive" / partition_name(old), self.compression)
        write_csv(new, tmp / "fresh.csv")
        arch = self.archive(key)
        if arch.exists():
            arch.replace(tmp / "old_archive")
        if (tmp / "archive").exists():
            arch.parent.mkdir(parents=True, exist_ok=True)
            (tmp / "archive").replace(arch)
        self.fresh(key).parent.mkdir(parents=True, exist_ok=True)
        (tmp / "fresh.csv").replace(self.fresh(key))
        shutil.rmtree(tmp, ignore_errors=True)

    def archive_aged(self, key: str) -> int:
        """Move fresh rows older than the cutoff to Parquet: write temp, verify, rename, then trim the CSV."""
        fresh = self.read_fresh(key)
        aged = fresh[fresh.Date < self.cutoff]
        if aged.empty:
            return 0
        new = aged[~aged.Date.isin(self.read_archive(key).Date)]  # crash recovery: already archived rows are dropped
        if not new.empty:
            final = self.archive(key) / partition_name(new)
            if final.exists():
                raise FileExistsError(f"archive partition already exists: {final}")
            write_parquet(new, final.with_name(final.name + ".pending"), self.compression)
            pending = final.with_name(final.name + ".pending")
            back = pd.read_parquet(pending).assign(Date=lambda d: d.Date.astype(str))
            if len(back) != len(new) or set(zip(back.Ticker, back.Date)) != set(zip(new.Ticker, new.Date)):
                pending.unlink()
                raise ValueError(f"archive verification failed for {key}")
            pending.replace(final)
        write_csv(fresh[fresh.Date >= self.cutoff], self.fresh(key))
        return len(aged)
