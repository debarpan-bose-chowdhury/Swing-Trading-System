"""Signals: Friday run that turns the index regime and per-bucket momentum into targets_{rebalance_date}.json."""

import json
import logging
import re
import shutil
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from app.analyst import costs, regime, selector
from app.analyst.common import Gate, Report, run_stage, tracked_symbol
from app.market import registry
from app.market.common import atomic, iso
from app.market.store import Store
from app.market.tradingcal import Calendar

SCHEMA_VERSION = 1
EXCLUDED = ("inactive", "noRowOnRebalanceDate", "insufficientHistory", "noPriceData", "illiquid")
TARGETS = re.compile(r"targets_(\d{4}-\d{2}-\d{2})\b")
# Holdings snapshot row fields. UNVERIFIED until the Angel One probe has been run (Ledger phase).
HOLDING_SYMBOL, HOLDING_LTP = "tradingsymbol", "ltp"


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def execution_date(cal: Calendar, rebalance: date) -> date:
    """First Monday-Friday NSE trading day after the rebalance date (the order is placed at its open)."""
    day = rebalance + timedelta(days=1)
    while day.weekday() >= 5 or not cal.is_trading_day(day):
        day += timedelta(days=1)
    return day


def is_last_attempt(now: datetime, cfg: dict) -> bool:
    """True for the final scheduled retry of the week (the one before `retryUntil`, e.g. Sun 22:00)."""
    until = time.fromisoformat(cfg["signals"]["retryUntil"].split()[-1])
    last = (datetime.combine(now.date(), until) - timedelta(minutes=cfg["signals"]["retryEveryMinutes"])).time()
    return now.weekday() == 6 and now.time() >= last


def check_gate(cfg: dict, cal: Calendar, rebalance: str) -> None:
    """Config problems raise ValueError (a failed run); data that is not ready yet raises Gate (retry later)."""
    if cfg["placeholders"]:
        raise ValueError("analyst.json still has placeholders: true; replace the example values and set it to false")
    year = f"{rebalance[:4]}-"
    if not any(h.startswith(year) for h in cal.holidays):
        raise ValueError(f"nse_calendar.json has no holidays for {rebalance[:4]}; a holiday would look like a trading day")
    market = Path(cfg["paths"]["market"])
    if (market / ".lock").exists():
        raise Gate("a Ticker Data run is in progress")
    status = read_json(market / "status.json")
    last = (status or {}).get("lastTradingDay")
    # The Archiver also writes this file (without a lastTradingDay); the index-row and missing-share checks then decide.
    if not status or status.get("status") not in ("ok", "partial") or (last is not None and last != rebalance):
        raise Gate(f"Ticker Data has not finished {rebalance}: {status and {k: status.get(k) for k in ('stage', 'status', 'lastTradingDay')}}")


def load_holdings(cfg: dict, now: datetime, known) -> dict:
    """Open tracked positions from the Ledger's book and their prices; {} parts when the Ledger has not run yet."""
    analyst = Path(cfg["paths"]["analyst"])
    status = read_json(analyst / "analyst_status.json") or {}
    good = (status.get("ledger") or {}).get("lastGoodRunDate")
    book_path = analyst / "ledger" / "book.csv"
    book = pd.read_csv(book_path, dtype={"ticker": str}) if book_path.exists() else pd.DataFrame(columns=["ticker", "qty", "avg_price"])
    ltp, snap_day = {}, None
    snaps = sorted((analyst / "snapshots").glob("holdings_*.json"))  # ISO dates in the names sort chronologically
    if snaps:
        snap_day = snaps[-1].stem.removeprefix("holdings_")
        for row in (read_json(snaps[-1]) or {}).get("rows", []):
            sym = tracked_symbol(str(row.get(HOLDING_SYMBOL, "")), known)
            if sym and row.get(HOLDING_LTP):
                ltp[sym] = float(row[HOLDING_LTP])
    limit = cfg["signals"]["maxHoldingsSnapshotAgeDays"]
    fresh = lambda day: day is not None and (now.date() - date.fromisoformat(day)).days <= limit  # noqa: E731
    return {"book": book, "ltp": ltp, "asOf": good, "available": book_path.exists() and fresh(good) and fresh(snap_day),
            "untracked": (status.get("ledger") or {}).get("untracked", [])}


def build_targets(cfg, cal, log, report, rebalance: str, history_row: pd.Series, now: datetime, holdings: dict | None, reg: pd.DataFrame) -> dict:
    market = Path(cfg["paths"]["market"])
    active = history_row.active_regime
    meta = json.loads(Path(cfg["paths"]["metadataConfig"]).read_text(encoding="utf-8"))
    store, rows = Store(market, cutoff=""), selector.rows_needed(cfg)
    c = cfg["costs"]

    buckets, bucket_of = {}, {}
    for b in (x["name"] for x in meta["filter"]["capBuckets"]):
        strategy = cfg["strategies"].get(active, {}).get(b)
        selecting = bool(strategy and strategy["top_n"] > 0 and cfg["composition"].get(b, 0) > 0)
        try:
            _, symbols = selector.bucket_universe(cfg, b, rebalance)
        except ValueError as e:
            if selecting:
                raise  # a bucket we select from must have a fresh universe
            log.warning("A bucket that is not selected from has no usable universe file; its held tickers will show bucket null")
            report.lines.append(f"WARNING {b}: {e}; held tickers in it show bucket null")
            symbols = []
        bucket_of.update({s: b for s in symbols})
        excluded = dict.fromkeys(EXCLUDED, 0)
        picks = []
        if selecting:
            known = [s for s in symbols if s in reg.index]
            live = [s for s in known if reg.at[s, "status"] == "active"]
            adj, value, no_data = selector.load_panel(store, live, rebalance, rows)
            picks, counts = selector.select_bucket(adj, value, rebalance, strategy, active, cfg["selector"])
            excluded.update(counts, inactive=len(known) - len(live), noPriceData=len(symbols) - len(known) + no_data)
            if symbols and counts["noRowOnRebalanceDate"] / len(symbols) > cfg["selector"]["maxMissingShare"]:
                raise Gate(f"{b}: {counts['noRowOnRebalanceDate']}/{len(symbols)} tickers have no row on {rebalance}")
        buckets[b] = {"strategy": strategy, "universe": len(symbols), "excluded": excluded, "selected": picks}

    book = holdings["book"] if holdings else pd.DataFrame(columns=["ticker", "qty", "avg_price"])
    held = {r.ticker: r for r in book.itertuples()}
    ltp = lambda t: (holdings["ltp"].get(t) or held[t].avg_price) if holdings else None  # noqa: E731
    available = bool(holdings and holdings["available"])
    selected = set()
    for b, entry in buckets.items():
        for p in entry["selected"]:
            selected.add(p["ticker"])
            notional = held[p["ticker"]].qty * ltp(p["ticker"]) if available and p["ticker"] in held else c["minTradeNotionalInr"]
            p["status"] = None if not available else "KEEP" if p["ticker"] in held else "ADD"
            p["estRoundTripCostInr"] = round(costs.round_trip(c, b, notional), 2)
            p["refNotionalInr"] = round(notional, 2)

    drops = []
    if available:
        for t, r in held.items():
            if t in selected:
                continue
            b = bucket_of.get(t)
            strategy = buckets.get(b, {}).get("strategy")
            no_allocation = b is not None and (not strategy or strategy["top_n"] == 0 or cfg["composition"].get(b, 0) == 0)
            reason = "UNKNOWN_REGIME" if active == "Unknown" else "NO_ALLOCATION" if no_allocation else "NOT_SELECTED"
            drops.append({"ticker": t, "bucket": b, "qty": int(r.qty), "avgCost": float(r.avg_price), "ltp": round(ltp(t), 2),
                          "reason": reason, "estExitCostInr": round(costs.exit_cost(c, b, r.qty, ltp(t)), 2)})

    invested = float((book.qty * book.avg_price).sum()) if holdings else 0.0
    market_value = float(sum(r.qty * ltp(t) for t, r in held.items())) if holdings else 0.0
    floating = float(cfg["capital"]["floatingCapitalInr"])
    targets = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": f"signals-{now.isoformat(timespec='seconds')}",
        "generatedAt": datetime.now(now.tzinfo).isoformat(timespec="seconds"),
        "status": "ok",
        "rebalanceDate": rebalance,
        "executionDate": iso(execution_date(cal, date.fromisoformat(rebalance))),
        "executionAt": "open",
        "regime": {"index": cfg["regime"]["index"], "raw": history_row.raw_regime, "active": active,
                   "pending": history_row.pending_regime, "pendingRemainingDays": int(history_row.pending_remaining_days),
                   "persistenceWeeks": cfg["regime"]["persistenceWeeks"]},
        "capital": {"floatingInr": floating, "investedCostInr": round(invested, 2), "investedMarketValueInr": round(market_value, 2),
                    "totalInr": round(floating + invested, 2), "holdingsAsOf": holdings["asOf"] if holdings else None},
        "composition": cfg["composition"],
        "limits": cfg["limits"],
        "buckets": buckets,
        "delta": {"available": available, "drop": drops},
        "untracked": holdings["untracked"] if holdings else [],
        "costModel": {"asOf": c["asOf"], "minTradeNotionalInr": c["minTradeNotionalInr"]},
    }
    report.block.update(rebalanceDate=rebalance, activeRegime=active, selectedCount=len(selected))
    report.lines += digest_lines(targets, holdings, cfg)
    return targets


def digest_lines(t: dict, holdings: dict | None, cfg: dict) -> list[str]:
    r = t["regime"]
    lines = [f"Gate: passed. Rebalance date {t['rebalanceDate']}, execution {t['executionDate']} at the open",
             f"Regime: raw {r['raw']}, active {r['active']}, pending {r['pending']} ({r['pendingRemainingDays']} days left)"]
    for b, e in t["buckets"].items():
        ex = ", ".join(f"{k} {v}" for k, v in e["excluded"].items() if v) or "none"
        lines.append(f"{b}: {len(e['selected'])} selected of {e['universe']}; excluded: {ex}")
    sel = [p["status"] for e in t["buckets"].values() for p in e["selected"]]
    if t["delta"]["available"]:
        lines.append(f"Delta: KEEP {sel.count('KEEP')}, ADD {sel.count('ADD')}, DROP {len(t['delta']['drop'])}")
    else:
        lines.append(f"WARNING: holdings unavailable or older than {cfg['signals']['maxHoldingsSnapshotAgeDays']} days; KEEP/DROP omitted")
    if t["untracked"]:
        lines.append("Untracked holdings: " + ", ".join(t["untracked"]))
    return lines


def write_targets(cfg: dict, targets: dict, now: datetime, force: bool) -> Path:
    folder = Path(cfg["paths"]["analyst"]) / "targets"
    path = folder / f"targets_{targets['rebalanceDate']}.json"
    if path.exists():
        if not force:
            raise FileExistsError(f"{path.name} already exists; use --force to supersede it")
        old, n = path.with_name(f"{path.stem}.superseded_{now.strftime('%H%M%S')}.json"), 1
        while old.exists():
            old, n = path.with_name(f"{path.stem}.superseded_{now.strftime('%H%M%S')}_{n}.json"), n + 1
        shutil.copy2(path, old)  # keep the original in place until the new file has replaced it
    atomic(path, lambda tmp: tmp.write_text(json.dumps(targets, indent=2), encoding="utf-8"))
    cutoff = iso(date.fromisoformat(targets["rebalanceDate"]) - timedelta(weeks=cfg["signals"]["targetsRetentionWeeks"]))
    for old in folder.glob("targets_*.json"):
        m = TARGETS.match(old.name)
        if m and m.group(1) < cutoff:
            old.unlink()
    return path


def run(cfg: dict, now: datetime, log: logging.Logger, report: Report, args) -> None:
    cal = Calendar(cfg["paths"]["calendar"])
    replay = args.as_of is not None
    if replay:
        date.fromisoformat(args.as_of)  # a malformed value fails here, not as a silent string comparison
    rebalance = None if replay else regime.live_rebalance_date(cal, now.date())
    if not replay and rebalance is None:
        log.info("No trading day this week; nothing to do")
        report.quiet = True
        return
    try:
        if not replay:
            rebalance = iso(rebalance)
            if (Path(cfg["paths"]["analyst"]) / "targets" / f"targets_{rebalance}.json").exists() and not args.force:
                log.info("targets_%s.json already exists; nothing to do", rebalance)
                report.quiet = True
                return
            check_gate(cfg, cal, rebalance)
        close = regime.index_close(cfg)
        history = regime.regime_history(close, cfg["regime"]["persistenceWeeks"])
        if replay:
            before = history.date[history.date <= args.as_of]
            if before.empty:
                raise ValueError(f"--as-of {args.as_of} is before the first rebalance date {history.date.iloc[0]}")
            rebalance = before.max()
        elif rebalance not in set(history.date):
            raise Gate(f"the {cfg['regime']['index']} series has no row for {rebalance}")
        row = history[history.date == rebalance].iloc[0]
        reg = registry.load(Path(cfg["paths"]["market"]) / "registry.csv")
        holdings = None if replay else load_holdings(cfg, now, reg.index)
        targets = build_targets(cfg, cal, log, report, rebalance, row, now, holdings, reg)
    except Gate as g:
        if replay or not is_last_attempt(now, cfg):
            raise
        report.error = f"no targets for week of {rebalance}: {g}"
        return
    if replay:
        report.quiet = True
        print(json.dumps(targets, indent=2))
        return
    regime_file = Path(cfg["paths"]["analyst"]) / "regime" / "regime_history.csv"
    atomic(regime_file, lambda tmp: history.to_csv(tmp, index=False))
    path = write_targets(cfg, targets, now, args.force)
    report.block["targetsFile"] = f"targets/{path.name}"
    log.info("wrote %s", path)


def add_args(parser) -> None:
    parser.add_argument("--as-of", help="read-only replay of the rebalance date on or before YYYY-MM-DD; writes nothing")


if __name__ == "__main__":
    run_stage("signals", run, extra_args=add_args)
