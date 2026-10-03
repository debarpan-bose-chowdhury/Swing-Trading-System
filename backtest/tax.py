"""Post-tax overlay. Decisions run on the pre-tax NAV exactly as live does; this module only reads the fills afterwards.

Lots are matched FIFO per ticker (the demat rule). Net P&L per lot piece = price difference less the buy and sell charges
allocated to it, the same "net_pl" convention as the live trading journal (so STT is treated as deductible: slightly conservative).
Rates come from the dated schedule in backtest.json by sale date. Within a financial year: short-term losses offset long-term
gains, long-term losses offset nothing, the long-term exemption applies once per year (against the lowest-rate gains first),
and tax = gains x the sale-date rates. The tax is deducted from the equity curve on the last simulated day of the financial year.

Not modelled: surcharge, loss carry-forward, the 31-Jan-2018 grandfathering of old holdings, dividend income tax.
"""

from collections import defaultdict, deque

import pandas as pd

from app.risk.common import is_date
from app.risk.tax import fy, holding_is_long


def row_at(schedule: list[dict], day: str) -> dict:
    """The schedule row in force on `day` (the first row for earlier dates)."""
    return next((r for r in reversed(schedule) if r["from"] <= day), schedule[0])


def lots(fills: pd.DataFrame) -> pd.DataFrame:
    """One row per (buy lot, sell) piece: ticker, buy_date, sell_date, qty, pnl, long (FIFO lot age), book_long (the live book's entry date)."""
    cols = ["ticker", "buy_date", "sell_date", "qty", "pnl", "long", "book_long"]
    if fills.empty:
        return pd.DataFrame(columns=cols)
    queues: dict[str, deque] = defaultdict(deque)
    out = []
    for f in fills.itertuples():
        if f.side == "BUY":
            queues[f.ticker].append([f.qty, f.price, f.trade_date, f.charges / f.qty])
            continue
        left, sell_cps = f.qty, f.charges / f.qty
        q = queues[f.ticker]
        while left > 0 and q:
            lot = q[0]
            take = min(left, lot[0])
            out.append({"ticker": f.ticker, "buy_date": lot[2], "sell_date": f.trade_date, "qty": take,
                        "pnl": (f.price - lot[1]) * take - (lot[3] + sell_cps) * take,
                        "long": holding_is_long(lot[2], f.trade_date),
                        "book_long": is_date(f.book_entry_date) and holding_is_long(f.book_entry_date, f.trade_date)})
            lot[0] -= take
            left -= take
            if lot[0] == 0:
                q.popleft()
    return pd.DataFrame(out, columns=cols)


def _scaled(weights: dict[int, float], total: float) -> dict[int, float]:
    """weights (positive gains per schedule row) scaled to sum to `total`."""
    s = sum(weights.values())
    return {k: v * total / s for k, v in weights.items()} if s > 0 and total > 0 else {}


def assess(pieces: pd.DataFrame, schedule: list[dict]) -> dict[str, dict]:
    """{financial year: report} with the estimated tax of every year that has a sale."""
    out = {}
    if pieces.empty:
        return out
    pieces = pieces.assign(fy=[fy(d) for d in pieces.sell_date], row=[schedule.index(row_at(schedule, d)) for d in pieces.sell_date])
    for year, g in pieces.groupby("fy"):
        end = row_at(schedule, f"{int(year[:4]) + 1}-03-31")
        st_all, lt_all = g[~g.long], g[g.long]
        st, lt = float(st_all.pnl.sum()), float(lt_all.pnl.sum())
        if st < 0 < lt:
            lt, st = max(lt + st, 0.0), 0.0
        st = max(st, 0.0)
        weights = lambda part: part[part.pnl > 0].groupby("row").pnl.sum().to_dict()  # noqa: E731
        st_tax = sum(schedule[i]["stcgPct"] * v for i, v in _scaled(weights(st_all), st).items())
        lt_parts, exemption = _scaled(weights(lt_all), lt), end["ltcgExemptionInr"]
        for i in sorted(lt_parts, key=lambda i: schedule[i]["ltcgPct"]):  # the exemption absorbs the lowest-rate gains first
            used = min(exemption, lt_parts[i])
            lt_parts[i] -= used
            exemption -= used
        lt_tax = sum(schedule[i]["ltcgPct"] * v for i, v in lt_parts.items())
        cess = end["cessPct"] * (st_tax + lt_tax)
        out[year] = {"fy": year, "estimate": True, "realisedNetPlInr": round(float(g.pnl.sum()), 2), "netShortTermGainInr": round(st, 2),
                     "netLongTermGainInr": round(lt, 2), "stcgTaxInr": round(st_tax, 2), "ltcgTaxInr": round(lt_tax, 2),
                     "cessInr": round(cess, 2), "taxInr": round(st_tax + lt_tax + cess, 2)}
    return out


def post_tax_curve(nav: pd.DataFrame, taxes: dict[str, dict]) -> pd.Series:
    """nav (date, nav columns) minus the cumulative tax, each year's tax taken on its financial year's last simulated day."""
    dates = list(nav.date)
    cum = pd.Series(0.0, index=dates)
    for year, rep in taxes.items():
        due = f"{int(year[:4]) + 1}-03-31"
        day = next((d for d in reversed(dates) if d <= due), dates[0])
        if day <= dates[-1] and (day >= dates[0]):
            cum[cum.index >= day] += rep["taxInr"]
    return pd.Series(nav.nav.to_numpy(float), index=dates) - cum


def entry_view_mismatch(pieces: pd.DataFrame) -> dict:
    """How often the live book's first-buy entry date and FIFO lot age disagree on short- vs long-term (decisions use the former)."""
    if pieces.empty:
        return {"pieces": 0, "mismatched": 0, "share": None}
    n = int((pieces.long != pieces.book_long).sum())
    return {"pieces": len(pieces), "mismatched": n, "share": round(n / len(pieces), 4)}
