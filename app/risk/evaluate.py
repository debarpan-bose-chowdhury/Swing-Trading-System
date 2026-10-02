"""Evaluate: verify the NAV rows Run wrote, append per-position rows, and on the stats day write statistics and the tax estimate.
Reporting only: it never changes signals or the state Run reads."""

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.analyst.journal import read as read_journal
from app.analyst.ledger import read_fills
from app.market.common import iso, write_csv
from app.market.store import Store
from app.market.tradingcal import Calendar
from app.risk import evaluator, nav, tax
from app.risk.common import SESSION_FINAL, WEEKDAYS, Gate, Report, history, read_json, risk_dir, run_stage, write_json
from app.risk.run import final_attempt


def run(cfg: dict, now: datetime, log: logging.Logger, report: Report, args) -> None:
    cal = Calendar(cfg["paths"]["calendar"])
    asof_d = cal.last_final_session(now, SESSION_FINAL)
    asof, risk = iso(asof_d), risk_dir(cfg)
    status = read_json(risk / "risk_status.json") or {}
    done = status.get("evaluate") or {}
    if done.get("status") == "ok" and done.get("asOf") == asof and not args.force:
        log.info("evaluation for %s already done", asof)
        report.quiet = True
        return
    report.block["asOf"] = asof
    sig = read_json(risk / "signals" / f"signals_{asof}.json")
    try:
        if not sig or sig.get("status") != "ok":
            raise Gate(f"Run has not written signals for {asof}")
    except Gate as g:
        if not final_attempt(cfg, now, asof_d):
            raise
        report.error = f"no NAV rows for {asof}: {g}"
        return
    rows = {name: nav.read_nav(risk / "nav" / f"nav_{name}.csv") for name in ("actual", "shadow")}
    missing = [n for n, df in rows.items() if (n == "actual" or cfg["shadow"]["enabled"]) and asof not in set(df.date)]
    if missing:
        report.error = f"NAV row for {asof} missing in: {', '.join(missing)}"
        return
    pos_file = risk / "nav" / "positions_daily.csv"
    old = evaluator.read_positions(pos_file)
    new = evaluator.positions_rows(asof, sig["positions"], Store(cfg["paths"]["market"], cutoff=""), history)
    positions = pd.concat([old[old.date != asof], pd.DataFrame(new, columns=evaluator.POSITION_COLS).astype(str)], ignore_index=True)
    write_csv(positions, pos_file)
    report.block.update(navRows=len(rows["actual"]))
    report.lines.append(f"NAV rows for {asof} present (actual {len(rows['actual'])}, shadow {len(rows['shadow'])}); {len(new)} position rows written")
    if WEEKDAYS[asof_d.weekday()] == cfg["evaluator"]["statsDay"]:
        weekly(cfg, cal, asof, sig, rows["actual"], positions, report)


def weekly(cfg: dict, cal: Calendar, asof: str, sig: dict, navs: pd.DataFrame, positions: pd.DataFrame, report: Report) -> None:
    risk, analyst = risk_dir(cfg), Path(cfg["paths"]["analyst"])
    journal = read_journal(analyst / "trading_journal.csv")
    fills = read_fills(analyst / "ledger" / "fills.csv")
    stats = evaluator.build(cfg, cal, asof, journal, fills, positions)
    write_json(risk / "reports" / f"stats_{asof}.json", stats)
    report.block["statsFile"] = f"reports/stats_{asof}.json"
    inception = stats["windows"].get("inception", {})
    a = inception.get("actual", {})
    report.lines.append(f"Statistics: return {a.get('totalReturn')}, max drawdown {a.get('maxDrawdown')}, Sharpe {a.get('sharpe')}, "
                        f"alpha {a.get('alphaAnnual')}, tracking gap {inception.get('trackingGap')}")
    book = [{"ticker": p["ticker"], "entry_date": next((b.entry_date for b in read_book(analyst).itertuples() if b.ticker == p["ticker"]), "UNKNOWN"),
             "avg": p["avgCostInr"], "close": p["closeInr"]} for p in sig["positions"]]
    reports = tax.estimate(journal, cfg["tax"]["rates"], navs, book, asof)
    for year, rep in reports.items():
        write_json(risk / "reports" / f"tax_{year}.json", rep)
    cur = reports[tax.fy(asof)]
    report.lines.append(f"Tax estimate {cur['fy']}: Rs {cur['estimatedTaxInr']:,.0f}; post-tax P/L Rs {cur['postTaxPlInr']:,.0f}")
    if cur["nearTwelveMonth"]:
        report.lines.append("Near 12 months: " + ", ".join(f"{n['ticker']} ({n['daysToTwelveMonths']} days, {n['unrealisedGainPct']:.0%})" for n in cur["nearTwelveMonth"]))


def read_book(analyst: Path) -> pd.DataFrame:
    path = analyst / "ledger" / "book.csv"
    return pd.read_csv(path, dtype=str, keep_default_na=False) if path.exists() else pd.DataFrame(columns=["ticker", "entry_date"])


if __name__ == "__main__":
    run_stage("evaluate", run)
