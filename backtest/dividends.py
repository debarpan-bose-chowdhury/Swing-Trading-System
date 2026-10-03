"""Cash dividends on the ex-date for the shares held at the previous close, credited to cash (so NAV and the TWR index carry them).

The stored series are split-adjusted and so is Yahoo's Dividends column, so quantity x amount needs no further adjustment.
Live shadow never credits dividends (it runs with no cash flows); the backtest does, which is the only intended difference.
Tax on dividends is not modelled (slab-rate income); the report says so.
"""

from collections import defaultdict
from pathlib import Path

import pandas as pd

from backtest.fills import Book


class Dividends:
    def __init__(self, table: pd.DataFrame | None = None):
        self.by_date: dict[str, list[tuple[str, float]]] = defaultdict(list)
        if table is not None:
            for t, d, a in zip(table.Ticker, table.ExDate, table.Amount.astype(float), strict=True):
                self.by_date[str(d)].append((t, a))

    @classmethod
    def load(cls, path: Path) -> "Dividends":
        return cls(pd.read_csv(path, dtype={"Ticker": str, "ExDate": str}) if path.exists() else None)

    def credit(self, book: Book, asof: str) -> list[dict]:
        """Credit today's dividends for the book's positions as they stood at the previous close. Call before the day's fills."""
        rows = []
        for t, amount in self.by_date.get(asof, ()):
            p = book.pos.get(t)
            if p and amount > 0:
                book.cash = round(book.cash + p["qty"] * amount, 2)
                rows.append({"date": asof, "ticker": t, "qty": int(p["qty"]), "perShare": amount, "amountInr": round(p["qty"] * amount, 2)})
        return rows
