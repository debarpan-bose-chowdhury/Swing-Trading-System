"""Next-open fills and the simulated book: the maths of app.risk.shadow.apply without its files.

shadow.apply re-reads every shadow signal file and rewrites fills.csv on each call, which is quadratic over an 18-year run, so this
keeps the same state in memory. tests/test_fills.py runs both side by side and requires identical fills, book and cash.

Mirrored on purpose, including its quirks:
- the price in the fills (and so the book's average cost) is rounded to 4 decimals; cash uses the unrounded price;
- cash is stored rounded to 2 decimals after every call and the next call starts from that figure, while the call's own
  caller (decide) sees the unrounded figure;
- an action is skipped when a FILL for the same ticker and side dated on or after the signal's execution date exists
  (a STOP repeats daily until the position is gone);
- a signal is retried for `carryOverDays` calendar days when the ticker has no Open, then dropped;
- the book is average-cost with the entry date of the first buy, like app.analyst.ledger.replay.
Realism layers (price bands, volume cap, circuit locks, settlement lag) are not here yet.
"""

import math
from datetime import date

import pandas as pd

from app.analyst import costs

BOOK_COLS = ["ticker", "qty", "avg_price", "entry_date", "entry_source"]


class Book:
    """Cash, positions and every fill of one simulated portfolio."""

    def __init__(self, cash: float):
        self.cash = round(cash, 2)
        self.pos: dict[str, dict] = {}  # ticker -> {qty, avg, entry_date}
        self.fills: list[dict] = []  # trade_date, ticker, bucket, side, qty, price, charges
        self.last_fill: dict[tuple[str, str], str] = {}

    def frame(self) -> pd.DataFrame:
        """The open book in the layout app.risk.run.Portfolio expects (what ledger.replay returns)."""
        rows = [{"ticker": t, "qty": int(p["qty"]), "avg_price": round(p["avg"], 4), "entry_date": p["entry_date"], "entry_source": "FILLS"}
                for t, p in sorted(self.pos.items())]
        return pd.DataFrame(rows, columns=BOOK_COLS).astype({"qty": "int64", "avg_price": "float64"})

    def _apply(self, f: dict) -> None:
        p = self.pos.get(f["ticker"])
        if f["side"] == "BUY":
            if p is None:
                p = self.pos[f["ticker"]] = {"qty": 0, "avg": 0.0, "entry_date": f["trade_date"]}
            p["avg"] = (p["qty"] * p["avg"] + f["qty"] * f["price"]) / (p["qty"] + f["qty"])
            p["qty"] += f["qty"]
        elif p is not None:
            p["qty"] -= min(f["qty"], p["qty"])
            if p["qty"] <= 0:
                del self.pos[f["ticker"]]


def execute(book: Book, c: dict, asof: str, signals: list[dict], open_price, carry_over_days: int) -> tuple[list[dict], list[str], float]:
    """Fill the pending signals at asof's Open.

    signals: dicts with asOf, executionDate and actions (ticker, bucket, side, qty), oldest first. open_price(ticker) is the raw
    Open on asof or None. Returns (fills, warnings, cash as decide must see it); book.cash is updated rounded to 2 decimals.
    """
    cash = book.cash
    held = {t: int(p["qty"]) for t, p in book.pos.items()}
    new: list[dict] = []
    warnings: list[str] = []
    for sig in signals:
        if sig["asOf"] >= asof or sig["executionDate"] > asof or (date.fromisoformat(asof) - date.fromisoformat(sig["asOf"])).days > carry_over_days:
            continue
        for a in sig["actions"]:
            t, side = a["ticker"], a["side"]
            if book.last_fill.get((t, side), "") >= sig["executionDate"] or any(x["ticker"] == t and x["side"] == side for x in new):
                continue
            op = open_price(t)
            if op is None or not math.isfinite(op) or op <= 0:
                warnings.append(f"SHADOW_NO_OPEN:{t}")
                continue
            bps = costs.slippage(c, a["bucket"], 1.0)
            price = op * (1 + bps) if side == "BUY" else op * (1 - bps)
            qty = int(a["qty"])
            if side == "SELL":
                qty = min(qty, held.get(t, 0))
            else:
                qty = min(qty, math.floor(cash / price))
                while qty and qty * price + costs.buy_charges(c, qty * price) > cash:
                    qty -= 1
                if qty < a["qty"]:
                    warnings.append(f"SHADOW_SHORTFALL:{t}")
            if qty <= 0:
                continue
            n = qty * price
            charges = costs.sell_charges(c, n) if side == "SELL" else costs.buy_charges(c, n)
            cash += n - charges if side == "SELL" else -(n + charges)
            held[t] = held.get(t, 0) + (qty if side == "BUY" else -qty)
            new.append({"trade_date": asof, "ticker": t, "bucket": a["bucket"], "side": side, "qty": qty, "price": round(price, 4), "charges": charges})
    for f in sorted(new, key=lambda f: f["side"] != "BUY"):  # ledger.replay processes a day's BUYs before its SELLs
        book._apply(f)
        book.last_fill[(f["ticker"], f["side"])] = asof
    book.fills += new
    book.cash = round(cash, 2)
    return new, list(dict.fromkeys(warnings)), cash
