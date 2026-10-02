"""Realised-gain tax estimate per financial year from trading_journal.csv (an estimate, not tax advice)."""

from datetime import date

import numpy as np
import pandas as pd

from app.risk.common import anniversary, is_date

NOTE = ("Estimate only, not tax advice. Surcharge, loss carry-forward, indexation, business-income treatment and the renumbered "
        "sections of the Income-tax Act 2025 are not modelled.")
TRUSTED = ("FILLS", "MANUAL_VERIFIED")
NEAR_DAYS = 56


def fy(day: str) -> str:
    """Financial year (April to March) of an ISO date, e.g. 2026-10-02 -> '2026-27'."""
    start = int(day[:4]) - (int(day[5:7]) < 4)
    return f"{start}-{(start + 1) % 100:02d}"


def holding_is_long(entry: str, exit_: str) -> bool:
    """Held more than 12 months."""
    return date.fromisoformat(exit_) > anniversary(date.fromisoformat(entry))


def _tax(st: float, lt: float, rates: dict) -> dict:
    """Net each class, set short-term losses off against long-term gains, tax the rest (long-term gain above the exemption)."""
    if st < 0 < lt:
        lt, st = max(lt + st, 0.0), 0.0
    st = max(st, 0.0)  # long-term losses never offset short-term gains
    stcg = rates["stcgPct"] * st
    ltcg = rates["ltcgPct"] * max(lt - rates["ltcgExemptionInr"], 0.0)
    tax = stcg + ltcg
    cess = rates["cessPct"] * tax
    return {"netShortTermGainInr": round(st, 2), "netLongTermGainInr": round(max(lt, 0.0), 2), "stcgTaxInr": round(stcg, 2),
            "ltcgTaxInr": round(ltcg, 2), "cessInr": round(cess, 2), "estimatedTaxInr": round(tax + cess, 2)}


def estimate(journal: pd.DataFrame, rates: dict, navs: pd.DataFrame, open_positions: list[dict], asof: str) -> dict[str, dict]:
    """{fy: report} for every financial year with trusted journal rows, plus the current year (near-12-month list)."""
    rows = journal[journal.source.isin(TRUSTED)]
    known = rows[np.array([is_date(e) and is_date(x) for e, x in zip(rows.entry_date, rows.exit_date, strict=True)], dtype=bool)].copy()
    excluded = sorted(set(rows.trade_id) - set(known.trade_id))
    known["pnl"] = known.net_pl.astype(float)
    known["fy"] = [fy(d) for d in known.exit_date]
    known["long"] = np.array([holding_is_long(e, x) for e, x in zip(known.entry_date, known.exit_date, strict=True)], dtype=bool)
    near = [{"ticker": p["ticker"], "entryDate": p["entry_date"], "daysToTwelveMonths": (anniversary(date.fromisoformat(p["entry_date"])) - date.fromisoformat(asof)).days,
             "unrealisedGainPct": round(p["close"] / p["avg"] - 1, 4)}
            for p in open_positions if is_date(p["entry_date"]) and 0 <= (anniversary(date.fromisoformat(p["entry_date"])) - date.fromisoformat(asof)).days <= NEAR_DAYS]
    out = {}
    for year in sorted(set(known.fy) | {fy(asof)}):
        g = known[known.fy == year]
        short, long = g[~g.long].pnl, g[g.long].pnl
        total = float(g.pnl.sum())
        year_navs = navs[np.array([fy(d) == year for d in navs.date], dtype=bool)].nav
        avg_nav = float(year_navs.mean()) if len(year_navs) else None
        result = _tax(float(short.sum()), float(long.sum()), rates)
        after = total - result["estimatedTaxInr"]
        out[year] = {"fy": year, "estimate": True, "note": NOTE, "rates": rates,
                     "shortTerm": {"gainsInr": round(float(short[short > 0].sum()), 2), "lossesInr": round(float(short[short < 0].sum()), 2)},
                     "longTerm": {"gainsInr": round(float(long[long > 0].sum()), 2), "lossesInr": round(float(long[long < 0].sum()), 2)},
                     **result, "realisedNetPlInr": round(total, 2), "postTaxPlInr": round(after, 2),
                     "postTaxPctOfAvgNav": round(after / avg_nav, 4) if avg_nav else None,
                     "excludedUnknownEntryDate": excluded, "nearTwelveMonth": near if year == fy(asof) else []}
    return out
