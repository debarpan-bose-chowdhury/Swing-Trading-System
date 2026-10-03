"""Backtest data preparation. Run from the repo root.

  python -m backtest.prep --check        config and app data paths (no network, no writes)
  python -m backtest.prep --dividends    fetch dividends and splits from Yahoo (the only network stage) -> backtest/data/
  python -m backtest.prep --scan         offline: calendar check and anomaly scan over app/data -> backtest/data/

Reads app/data and app/config only; every output goes to backtest/data/. Exit codes: 0 ok, 1 failed, 3 input missing (run the upstream stage).
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from app.market import common as market_common
from app.market.fetcher import Fetcher
from app.market.tradingcal import Calendar
from backtest import checks, config, pit

log = logging.getLogger("backtest.prep")


class MissingInput(Exception):
    """Upstream data the stage needs is not there yet (exit 3)."""


def out_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"])


def load_pit(cfg: dict) -> pit.PitData:
    app_data = Path(cfg["paths"]["appData"])
    names = list(cfg["capital"]["composition"])
    data = pit.PitData.load(app_data, names)
    if not data.series or data.index.empty:
        raise MissingInput(f"no stored prices or no {pit.INDEX_KEY} index under {app_data}/market; run the Ticker Data stages first")
    return data


def fetch_dividends(cfg: dict, symbols: list[str], today: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(dividends [Ticker, ExDate, Amount], splits [Ticker, ExDate, Ratio]) for the symbols from Yahoo, or raises when a batch fails."""
    mkt = market_common.load_config()
    fx, frames, failed = Fetcher(mkt, log), [], []
    end = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
    for chunk, frame in fx.batches([f"{s}.NS" for s in symbols], mkt["fetch"]["batchSize"], start=cfg["prep"]["dividendsFrom"], end=end):
        if frame is None:
            failed += chunk
        else:
            frames.append(frame)
    if failed:
        raise RuntimeError(f"Yahoo fetch failed for {len(failed)} symbols (first: {failed[:3]}); nothing written, re-run later")
    all_ = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["Ticker", "Date", "Dividends", "Splits"])
    all_["Ticker"] = all_.Ticker.str.removesuffix(".NS")
    div = all_[all_.Dividends > 0].rename(columns={"Date": "ExDate", "Dividends": "Amount"})[["Ticker", "ExDate", "Amount"]]
    spl = all_[all_.Splits > 0].rename(columns={"Date": "ExDate", "Splits": "Ratio"})[["Ticker", "ExDate", "Ratio"]]
    return div.sort_values(["Ticker", "ExDate"], ignore_index=True), spl.sort_values(["Ticker", "ExDate"], ignore_index=True)


def run_dividends(cfg: dict, today: str) -> None:
    data = load_pit(cfg)
    div, spl = fetch_dividends(cfg, sorted(data.series), today)
    market_common.write_csv(div, out_dir(cfg) / "dividends.csv")
    market_common.write_csv(spl, out_dir(cfg) / "splits.csv")
    log.info("dividends: %d rows for %d tickers; splits: %d rows", len(div), div.Ticker.nunique(), len(spl))


def run_scan(cfg: dict) -> dict:
    data = load_pit(cfg)
    path = out_dir(cfg) / "dividends.csv"
    divs = pd.read_csv(path, dtype={"Ticker": str, "ExDate": str}) if path.exists() else None
    cal = checks.calendar_check(Calendar(Path(cfg["paths"]["appConfig"]) / "nse_calendar.json"), list(data.index.Date))
    anomalies = checks.scan(data.series, divs, cfg["prep"])
    market_common.write_csv(anomalies, out_dir(cfg) / "anomalies.csv")
    report = {
        "universe": {b: len(s) for b, s in data.buckets.items()}, "withHistory": len(data.series), "noHistory": data.missing(),
        "firstDate": {t: df.Date.iloc[0] for t, df in data.series.items()}, "dividendsLoaded": divs is not None,
        "calendar": cal, "anomalies": anomalies.Kind.value_counts().to_dict(), "dataHash": data.data_hash()}
    market_common.atomic(out_dir(cfg) / "data_report.json", lambda tmp: tmp.write_text(json.dumps(report, indent=2), encoding="utf-8"))
    log.info("scan: %d tickers, %d anomalies %s; calendar: %d index days the calendar calls closed, %d trading days without an index row",
             len(data.series), len(anomalies), report["anomalies"], len(cal["notTrading"]), len(cal["noIndexRow"]))
    if divs is None:
        log.warning("dividends.csv not found: dividend checks skipped (run --dividends)")
    return report


def check() -> int:
    try:
        cfg = config.load()
        names = list(cfg["capital"]["composition"])
        app_data = Path(cfg["paths"]["appData"])
        if not (app_data / "market").exists():
            print(f"prep: check ok (no {app_data}/market yet: run the Ticker Data stages before --scan)")
            return 0
        found = {b: len(s) for b, s in pit.read_universe(app_data / "storage", names).items()}
        print(f"prep: check ok (universe {found})")
        return 0
    except Exception as e:
        print(f"prep: check FAILED: {e!r}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.prep")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--dividends", action="store_true")
    group.add_argument("--scan", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = config.load()
        if args.dividends:
            run_dividends(cfg, datetime.now(market_common.IST).date().isoformat())
        else:
            run_scan(cfg)
    except MissingInput as e:
        log.error("%s", e)
        return 3
    except Exception:
        log.exception("prep failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
