"""Ledger: end-of-day mirror of the Angel One portfolio into fills, the open-position book and the trading journal.

`ledger/fills.csv` is the single source of truth (append-only). The book and the journal's rows are replayed from it, so
a crash between writes cannot leave them disagreeing: the next run rebuilds both. Reconciliation against the broker's
holdings adds adjustment rows (corporate action, missed buy, estimated sell) to the same file. It never places an order.
"""

import hashlib
import json
import logging
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.analyst import journal, secrets
from app.analyst.broker import BROKER_SECRETS, EXPECTED, Broker, BrokerError, LoginFailed
from app.analyst.common import Report, run_stage, tracked_symbol
from app.market import registry
from app.market.common import atomic, iso, write_csv
from app.market.tradingcal import Calendar

FILL_COLS = ["fill_key", "trade_date", "ticker", "broker_symbol", "side", "qty", "price", "fill_time", "order_id", "run_id", "kind"]
BOOK_COLS = ["ticker", "qty", "avg_price", "entry_date", "entry_source", "last_reconciled"]
# Adjustment rows apply after the day's real fills; seeds sort by their (older) entry date.
KIND_RANK = {"SEED": 0, "FILL": 1, "UNKNOWN": 2, "BROKER_AVG": 2, "CORP_ACTION": 2, "ESTIMATED": 2}
ENTRY_SOURCE = {"FILL": "FILLS", "SEED": "SEED", "UNKNOWN": "UNKNOWN", "BROKER_AVG": "BROKER_AVG"}
FETCH_ORDER = ("tradebook", "positions", "holdings", "funds")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- fills -------------------------------------------------------------------------------------------------------
def read_fills(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=FILL_COLS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.astype({"qty": "int64", "price": "float64"})[FILL_COLS]


def parse_tradebook(rows: list[dict], run_date: str, run_id: str, known, ignore: set) -> tuple[list[dict], set, int]:
    """Today's NSE DELIVERY tradebook rows of tracked tickers as canonical fills; also the untracked symbols and skipped rows."""
    if rows and (missing := [f for f in EXPECTED["tradebook"] if f not in rows[0]]):
        raise ValueError(f"tradebook field(s) not found: {', '.join(missing)}; run the probe and fix the mapping")
    fills, untracked, skipped = [], set(), 0
    for r in rows:
        if str(r["exchange"]).upper() != "NSE" or str(r["producttype"]).upper() != "DELIVERY":
            skipped += 1
            continue
        ticker = tracked_symbol(str(r["tradingsymbol"]), known)
        if ticker is None or ticker in ignore:
            untracked.add(str(r["tradingsymbol"]))
            continue
        qty, price, when = int(float(r["fillsize"])), float(r["fillprice"]), str(r["filltime"] or "")
        key = str(r["fillid"] or "") or hashlib.sha256(f"{r['orderid']}|{when}|{qty}|{price}".encode()).hexdigest()
        fills.append({"fill_key": f"{run_date}-{key}", "trade_date": run_date, "ticker": ticker, "broker_symbol": r["tradingsymbol"],
                      "side": str(r["transactiontype"]).upper(), "qty": qty, "price": price,
                      "fill_time": f"{run_date}T{when}+05:30" if len(when) == 8 else "", "order_id": str(r["orderid"] or ""),
                      "run_id": run_id, "kind": "FILL"})
    return fills, untracked, skipped


def append_fills(path: Path, existing: pd.DataFrame, new: list[dict]) -> tuple[pd.DataFrame, int]:
    new = [f for f in new if f["fill_key"] not in set(existing.fill_key)]
    if not new:
        return existing, 0
    out = pd.concat([existing, pd.DataFrame(new, columns=FILL_COLS)], ignore_index=True)
    write_csv(out, path)
    return out, len(new)


# --- replay ------------------------------------------------------------------------------------------------------
def replay(fills: pd.DataFrame) -> tuple[pd.DataFrame, list[dict], list[str]]:
    """(open book, closed sells, anomalies) from the fills, by average-cost matching in date / fill-time order.

    BUY adds at a weighted average cost; SET (corporate action) replaces quantity and cost; SELL reduces the quantity
    (capped at the book) and is journaled once per ticker per day and source, valued at the book's average cost.
    """
    order = fills.assign(seq=range(len(fills)), rank=fills.kind.map(KIND_RANK), when=fills.fill_time,
                         srank=fills.side.map({"BUY": 0, "SET": 0, "SELL": 1})).sort_values(["trade_date", "rank", "when", "srank", "seq"])
    pos, groups, anomalies, count = {}, {}, [], {}
    for r in order.itertuples():
        p = pos.get(r.ticker)
        if r.side == "BUY":
            if p is None:
                pos[r.ticker] = p = {"qty": 0, "avg": 0.0, "entry_date": "UNKNOWN" if r.kind == "UNKNOWN" else r.trade_date,
                                     "entry_source": ENTRY_SOURCE[r.kind]}
            elif r.kind == "BROKER_AVG":
                p["entry_source"] = "BROKER_AVG"
            p["avg"] = (p["qty"] * p["avg"] + r.qty * r.price) / (p["qty"] + r.qty)
            p["qty"] += r.qty
        elif r.side == "SET":
            p = pos.setdefault(r.ticker, {"qty": 0, "avg": 0.0, "entry_date": "UNKNOWN", "entry_source": "UNKNOWN"})
            p["qty"], p["avg"] = r.qty, r.price
        else:  # SELL
            if p is None:
                anomalies.append(f"SELL {r.qty} {r.ticker} on {r.trade_date} with no book position")
                continue
            sold = min(r.qty, p["qty"])
            if r.qty > p["qty"]:
                anomalies.append(f"SELL {r.qty} {r.ticker} on {r.trade_date} exceeds the book quantity {p['qty']}; capped")
            source = "ESTIMATED" if r.kind == "ESTIMATED" else "FILLS"
            g = groups.get((r.ticker, r.trade_date, source))
            if g is None:
                count[(r.ticker, r.trade_date)] = n = count.get((r.ticker, r.trade_date), 0) + 1
                g = groups[(r.ticker, r.trade_date, source)] = {
                    "ticker": r.ticker, "exit_date": r.trade_date, "n": n, "source": source, "qty": 0, "value": 0.0,
                    "entry_price": p["avg"], "entry_date": p["entry_date"], "entry_source": p["entry_source"]}
            g["qty"] += sold
            g["value"] += sold * r.price
            p["qty"] -= sold
        if p is not None and p["qty"] <= 0:
            del pos[r.ticker]
    sells = [{**g, "exit_price": g["value"] / g["qty"]} for g in groups.values() if g["qty"] > 0]
    book = pd.DataFrame([{"ticker": t, "qty": int(p["qty"]), "avg_price": round(p["avg"], 4), "entry_date": p["entry_date"],
                          "entry_source": p["entry_source"]} for t, p in sorted(pos.items())],
                        columns=BOOK_COLS[:-1])
    return book, sells, anomalies


# --- reconciliation ----------------------------------------------------------------------------------------------
def observed_positions(holdings: list[dict], positions: list[dict], cfg: dict, known, ignore: set) -> dict[str, tuple[int, float]]:
    """ticker -> (quantity, average price) the broker reports. Holdings first; shares bought today that holdings do not
    show yet come from the DELIVERY position's net quantity. Which holdings fields count is `ledger.observedQtyFields`."""
    fields, out = cfg["ledger"]["observedQtyFields"], {}
    for h in holdings:
        t = tracked_symbol(str(h["tradingsymbol"]), known)
        if t is None or t in ignore or str(h.get("exchange", "NSE")).upper() != "NSE":
            continue
        qty = int(sum(float(h.get(f) or 0) for f in fields))
        if qty > 0:
            q0, a0 = out.get(t, (0, 0.0))
            out[t] = (q0 + qty, (q0 * a0 + qty * float(h["averageprice"])) / (q0 + qty))
    for p in positions:
        t = tracked_symbol(str(p["tradingsymbol"]), known)
        if t is None or t in ignore or t in out or str(p["producttype"]).upper() != "DELIVERY":
            continue
        qty = int(float(p["netqty"] or 0))
        if qty > 0:
            out[t] = (qty, float(p.get("avgnetprice") or p.get("averageprice") or 0))
    return out


def reconcile(book: pd.DataFrame, observed: dict, tol: float, run_date: str, run_id: str, last_ltp, first_run: bool) -> tuple[list[dict], list[str], set]:
    """Adjustment fills that make the book match the broker, digest lines, and the tickers that now agree."""
    held = {r.ticker: (r.qty, r.avg_price) for r in book.itertuples()}
    rows, lines, done = [], [], set()

    def add(t, side, qty, price, kind):
        rows.append({"fill_key": f"ADJ-{run_date}-{t}-{kind}", "trade_date": run_date, "ticker": t, "broker_symbol": "", "side": side,
                     "qty": int(qty), "price": float(price), "fill_time": "", "order_id": "", "run_id": run_id, "kind": kind})

    for t in sorted(set(held) | set(observed)):
        bq, ba = held.get(t, (0, 0.0))
        oq, oa = observed.get(t, (0, 0.0))
        done.add(t)
        if oq == bq:
            continue
        if bq > 0 and oq > 0 and abs(oq * oa - bq * ba) <= tol * bq * ba:
            add(t, "SET", oq, oa, "CORP_ACTION")
            lines.append(f"CORP_ACTION {t}: quantity {bq} -> {oq}, average {ba:.2f} -> {oa:.2f}")
        elif oq > bq:
            extra = oq - bq
            price = (oq * oa - bq * ba) / extra
            kind = "UNKNOWN" if first_run and bq == 0 else "BROKER_AVG"
            add(t, "BUY", extra, price if price > 0 else oa, kind)
            lines.append(f"{'UNSEEDED' if kind == 'UNKNOWN' else 'MISSED_BUY'} {t}: book {bq} -> broker {oq}")
        else:
            add(t, "SELL", bq - oq, last_ltp(t) or ba, "ESTIMATED")
            lines.append(f"MISSED_SELL {t}: {bq - oq} share(s) written as an ESTIMATED journal row; correct it from the contract note")
    return rows, lines, done


def seed_fills(path: Path, known, ignore: set, run_id: str) -> tuple[list[dict], list[str]]:
    """One SEED buy per tracked row of seed_positions.csv (ticker, qty, entry_date, entry_price)."""
    if not path.exists():
        return [], []
    fills, skipped = [], []
    for r in pd.read_csv(path, dtype=str, keep_default_na=False).itertuples():
        t = r.ticker.strip()
        if not t:
            continue
        if t not in known or t in ignore:
            skipped.append(t)
            continue
        fills.append({"fill_key": f"SEED-{t}", "trade_date": r.entry_date, "ticker": t, "broker_symbol": "", "side": "BUY", "qty": int(float(r.qty)),
                      "price": float(r.entry_price), "fill_time": "", "order_id": "", "run_id": run_id, "kind": "SEED"})
    return fills, skipped


# --- housekeeping ------------------------------------------------------------------------------------------------
def housekeeping(analyst: Path, today: date, cfg: dict) -> list[str]:
    notes = []
    lc = cfg["ledger"]
    dest = analyst / "backup" / iso(today)
    dest.mkdir(parents=True, exist_ok=True)
    for src in (analyst / "ledger" / "fills.csv", analyst / "ledger" / "book.csv", analyst / "trading_journal.csv"):
        if src.exists():
            shutil.copy2(src, dest / src.name)
    for folder in (analyst / "backup").iterdir():
        if folder.is_dir() and folder.name < iso(today - timedelta(days=lc["backupRetentionDays"])):
            shutil.rmtree(folder, ignore_errors=True)
    snaps = analyst / "snapshots"
    for kind in FETCH_ORDER:
        files = sorted(snaps.glob(f"{kind}_*.json"))
        for f in files[:-1]:  # the newest of each type is always kept
            if f.stem.removeprefix(f"{kind}_") < iso(today - timedelta(days=lc["snapshotRetentionDays"])):
                f.unlink()
    return notes


def missed_days(cal: Calendar, analyst: Path, last_good: str | None, today: date) -> list[str]:
    """Trading days after the last good run and before today for which no snapshot exists."""
    if not last_good:
        return []
    start = date.fromisoformat(last_good) + timedelta(days=1)
    return [iso(d) for d in cal.days(start, today - timedelta(days=1)) if not any((analyst / "snapshots").glob(f"*_{d}.json"))]


def last_known_ltp(analyst: Path, today: str, known):
    """ltp from the newest holdings snapshot before today that shows the ticker."""
    files = sorted((f for f in (analyst / "snapshots").glob("holdings_*.json") if not f.stem.endswith(today)), reverse=True)

    def find(ticker: str) -> float | None:
        for f in files:
            for row in (read_json(f) or {}).get("rows", []):
                if tracked_symbol(str(row.get("tradingsymbol", "")), known) == ticker and row.get("ltp"):
                    return float(row["ltp"])
        return None

    return find


# --- run ---------------------------------------------------------------------------------------------------------
def run(cfg: dict, now: datetime, log: logging.Logger, report: Report, args, broker: Broker | None = None) -> None:
    today, analyst = iso(now.date()), Path(cfg["paths"]["analyst"])
    cal = Calendar(cfg["paths"]["calendar"])
    status = (read_json(analyst / "analyst_status.json") or {}).get("ledger", {})
    if not args.force and (not cal.is_trading_day(today) or (status.get("runDate") == today and status.get("status") == "ok")):
        log.info("%s: not a trading day or already done; nothing to do", today)
        report.quiet = True
        return
    run_id = f"ledger-{now.isoformat(timespec='seconds')}"
    reg = registry.load(Path(cfg["paths"]["market"]) / "registry.csv")
    known, ignore = reg.index, set(cfg["capital"]["ignoreSymbols"])
    lines, fetched, failed = report.lines, {}, []

    broker = broker or Broker(cfg, secrets.load(BROKER_SECRETS), log)
    try:
        broker.login()
        for name in FETCH_ORDER:
            try:
                fetched[name] = getattr(broker, name)()
            except BrokerError as e:
                failed.append(f"{name} ({e.code})")
                log.warning("%s fetch failed: %r", name, e.code)
                continue
            snap = {"fetchedAt": now.isoformat(timespec="seconds"), "endpoint": name, "rows": fetched[name]}
            atomic(analyst / "snapshots" / f"{name}_{today}.json", lambda tmp, s=snap: tmp.write_text(json.dumps(s), encoding="utf-8"))
    except LoginFailed as e:
        report.error = str(e)
        return
    finally:
        broker.logout()
    lines.append("Fetched: " + ", ".join(n for n in FETCH_ORDER if n in fetched) + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    report.partial = bool(failed)

    fills_path = analyst / "ledger" / "fills.csv"
    fills = read_fills(fills_path)
    new, untracked = [], set()
    if "tradebook" in fetched:
        try:
            new, untracked, _ = parse_tradebook(fetched["tradebook"], today, run_id, known, ignore)
        except ValueError as e:  # the raw snapshot is already saved for the probe / parser fix
            report.error = str(e)
            return
    first_run = not (analyst / "seed.done").exists()
    if first_run:
        seeds, skipped = seed_fills(Path(cfg["paths"]["seed"]), known, ignore, run_id)
        new += seeds
        if skipped:
            lines.append("Seed rows for untracked tickers ignored: " + ", ".join(skipped))
    fills, recorded = append_fills(fills_path, fills, new)

    book, sells, anomalies = replay(fills)
    reconciled = set()
    if "holdings" in fetched:
        observed = observed_positions(fetched["holdings"], fetched.get("positions", []), cfg, known, ignore)
        untracked |= {str(h["tradingsymbol"]) for h in fetched["holdings"] if tracked_symbol(str(h["tradingsymbol"]), known) is None}
        adj, adj_lines, reconciled = reconcile(book, observed, cfg["ledger"]["corpActionCostTolerance"], today, run_id,
                                               last_known_ltp(analyst, today, known), first_run)
        lines += adj_lines
        fills, extra = append_fills(fills_path, fills, adj)
        recorded += extra
        if extra:
            book, sells, anomalies = replay(fills)
        if first_run:
            atomic(analyst / "seed.done", lambda tmp: tmp.write_text(today, encoding="utf-8"))
    else:
        lines.append("Holdings not fetched: reconciliation skipped; the next run reconciles by diff")

    old = pd.read_csv(analyst / "ledger" / "book.csv", dtype=str, keep_default_na=False) if (analyst / "ledger" / "book.csv").exists() else None
    carried = dict(zip(old.ticker, old.last_reconciled)) if old is not None and "last_reconciled" in old else {}
    book["last_reconciled"] = [today if t in reconciled else carried.get(t, "") for t in book.ticker]
    write_csv(book, analyst / "ledger" / "book.csv")

    try:
        result = journal.sync(analyst / "trading_journal.csv", sells, cfg["costs"], run_id)
    except OSError as e:  # e.g. the journal is open in Excel; the rows are rebuilt from fills on the next run
        log.warning("journal not written: %r", e)
        report.partial = True
        lines.append("Journal not written (file locked?); it catches up on the next run")
        result = {"added": 0, "promoted": [], "unreadable": [], "estimated": []}
    for key, title in (("promoted", "Journal rows promoted to MANUAL_VERIFIED"), ("unreadable", "Journal rows with unreadable edits")):
        if result[key]:
            lines.append(f"{title}: {', '.join(result[key])}")
    if result["estimated"]:
        lines.append("ESTIMATED journal rows awaiting your correction: " + ", ".join(result["estimated"]))
    lines += [f"ANOMALY {a}" for a in anomalies]
    gaps = missed_days(cal, analyst, status.get("lastGoodRunDate"), now.date())
    if gaps:
        lines.append("Trading days with no snapshot (fills could not be fetched): " + ", ".join(gaps))
    if untracked:
        lines.append("Untracked: " + ", ".join(sorted(untracked)))
    try:
        housekeeping(analyst, now.date(), cfg)
    except OSError as e:
        log.warning("housekeeping failed: %r", e)
        lines.append("Backup/purge failed; see the log")
    lines.append(f"Fills recorded {recorded}; journal rows added {result['added']}; open positions {len(book)}")
    report.block.update(fillsRecorded=recorded, journalRowsAdded=result["added"], estimatedRows=len(result["estimated"]),
                        missedTradingDays=gaps, untracked=sorted(untracked))


if __name__ == "__main__":
    run_stage("ledger", run)
