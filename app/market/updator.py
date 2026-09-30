"""Updator: daily registry refresh, gap-fill of new trading days, corporate-action rebuilds."""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from app.market import calendar_sync, registry
from app.market.common import Report, iso, run_stage, shift
from app.market.fetcher import Fetcher
from app.market.ingest import backfill, build_series, fetch_valid, too_many_rejects
from app.market.tradingcal import Calendar

PRICES = ["Open", "High", "Low", "Close"]


class Plan(NamedTuple):
    start: str  # earliest date to request
    gaps: set  # trading days (ISO) expected but not stored
    stored: pd.DataFrame  # current fresh rows, for the restatement check


def plan(s, cal: Calendar, cfg: dict, today: date, end: date) -> Plan | None:
    """Gaps for one stored series: days after the last stored date plus missing days in the lookback window.

    The request starts at the last stored date (one day of overlap) so a restated history is noticed.
    """
    last = s.store.last_date(s.key)
    fresh = s.store.read_fresh(s.key)
    first = date.fromisoformat(fresh.Date.min() if not fresh.empty else last)
    window = max(today - timedelta(days=cfg["updator"]["lookbackDays"]), first)
    have = set(fresh.Date)
    days = set(cal.days(window, end)) | set(cal.days(date.fromisoformat(last) + timedelta(days=1), end))
    gaps = {iso(d) for d in days if iso(d) not in have}
    return Plan(min(min(gaps), last), gaps, fresh) if gaps else None


def actions(raw: pd.DataFrame, p: Plan) -> str | None:
    """Reason a full rebuild is needed: split/dividend on a new day, or stored prices restated by Yahoo."""
    ev = raw[raw.Date.isin(p.gaps) & ((raw.Dividends > 0) | (raw.Splits > 0))]
    if not ev.empty:
        return ", ".join(f"{'split' if r.Splits > 0 else 'dividend'} {r.Date}" for r in ev.itertuples())
    both = raw.merge(p.stored, on="Date", suffixes=("", "_old"))
    if any(not np.allclose(both[c], both[f"{c}_old"], rtol=1e-4) for c in PRICES):
        return "stored prices restated by Yahoo"
    return None


def run(cfg: dict, now: datetime, log: logging.Logger, report: Report, fetcher: Fetcher | None = None) -> None:
    market = Path(cfg["paths"]["market"])
    today = now.date()
    fx = fetcher or Fetcher(cfg, log)
    calendar_sync.sync(cfg, fx, log, report, today, full=False)
    cal = Calendar(cfg["paths"]["calendar"])
    end = cal.last_final_session(now, cfg["fetch"]["sessionFinalAfterIST"])
    report.last_trading_day = iso(end)

    reg = registry.load(market / "registry.csv")
    report.registry_refreshed = registry.upstream_ok(cfg, iso(today))
    if report.registry_refreshed:
        back = registry.refresh(reg, registry.upstream_symbols(cfg))
        report.notes += [f"re-activated {t}" for t in back]
    else:
        log.warning("Upstream not healthy today; registry not refreshed")
        report.notes.append("registry not refreshed: upstream health.json missing, unhealthy or stale")
    registry.save(reg, market / "registry.csv")

    active = list(reg.index[reg.status == "active"])
    series = build_series(cfg, active, shift(iso(today), -cfg["archiver"]["cutoffDays"]))
    plans, no_history = {}, []
    for s in series:
        if s.store.last_date(s.key) is None:
            no_history.append(s)
        elif p := plan(s, cal, cfg, today, end):
            plans[s.ticker] = p
    by_ticker = {s.ticker: s for s in series}
    new_tickers = {s.ticker for s in no_history}
    rebuild, new_rows = list(no_history), {}

    if plans:
        start = min(p.start for p in plans.values())
        for s, valid, rej, raw in fetch_valid(fx, cal, cfg, report, [by_ticker[t] for t in plans], iso(end), start=start, end=shift(iso(end), 1)):
            p = plans[s.ticker]
            if reason := actions(raw, p):
                report.rebuilt[s.ticker] = reason
                rebuild.append(s)
                continue
            new_rows[s.ticker] = int(raw.Date.isin(p.gaps).sum())
            if too_many_rejects(cfg, len(rej), len(raw)):
                report.failed[s.ticker] = f"rolled back: {len(rej)}/{len(raw)} rows rejected"
                continue
            try:
                if not valid.empty:
                    s.store.upsert(s.key, valid)
                    report.updated.add(s.ticker)
            except Exception as e:
                report.failed[s.ticker] = f"write failed: {e!r}"

    got = backfill(fx, cal, cfg, report, rebuild, iso(end)) if rebuild else {}
    new_rows.update({t: n for t, n in got.items() if t in new_tickers})
    track_no_data(cfg, reg, new_rows, plans, new_tickers, iso(end), iso(today), report, log)
    registry.save(reg, market / "registry.csv")


def track_no_data(cfg, reg, new_rows, plans, new_tickers, end, today, report, log) -> None:
    """Count consecutive trading days with an empty response; set inactive at the threshold.

    Skipped when nothing came back for anyone (a systemic outage, not dead tickers).
    """
    if not any(new_rows.values()):
        if new_rows:
            log.warning("No ticker returned any new rows; skipping no-data tracking")
        return
    for t, n in new_rows.items():
        if t not in reg.index or t in report.failed or not (t in new_tickers or end in plans[t].gaps):
            continue
        reg.at[t, "no_data_days"] = 0 if n else reg.at[t, "no_data_days"] + 1
        if reg.at[t, "no_data_days"] >= cfg["updator"]["deadTickerNoDataDays"]:
            reg.loc[t, ["status", "inactive_since", "absent_since_inactive"]] = ["inactive", today, False]
            report.inactive.append(t)


if __name__ == "__main__":
    run_stage("updator", run)
