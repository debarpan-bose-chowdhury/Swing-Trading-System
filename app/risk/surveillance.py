"""Surveillance stage: download the NSE ASM / GSM / trade-for-trade / price-band lists and store the normalised file."""

import logging
from datetime import datetime
from pathlib import Path

from app.market.common import atomic, iso
from app.market.tradingcal import Calendar
from app.metadata.data_source import NseClient
from app.risk import surveil
from app.risk.common import Report, read_json, risk_dir, run_stage, write_json

NSE_HOME = "https://www.nseindia.com"  # fetched first so the session carries NSE's cookies


def download(cfg: dict, client: NseClient, log: logging.Logger, asof: str, folder: Path) -> dict:
    """source -> payload bytes, or None when the download failed after its retries. Raw payloads are kept."""
    try:
        client.get(NSE_HOME)
    except Exception as e:
        log.warning("NSE home page not reachable: %r", e)
    payloads = {}
    for name in surveil.SOURCES:
        src = cfg["surveillance"]["sources"][name]
        try:
            payloads[name] = client.get(src["url"])
            atomic(folder / "raw" / f"{name}_{asof}.{src['format']}", lambda tmp, p=payloads[name]: tmp.write_bytes(p))
        except Exception as e:
            log.warning("%s download failed: %r", name, e)
            payloads[name] = None
    return payloads


def run(cfg: dict, now: datetime, log: logging.Logger, report: Report, args, client: NseClient | None = None) -> None:
    asof = iso(now.date())
    if not Calendar(cfg["paths"]["calendar"]).is_trading_day(asof):
        log.info("%s is not a trading day; nothing to do", asof)
        report.quiet = True
        return
    folder = risk_dir(cfg) / "surveillance"
    out = folder / f"surveillance_{asof}.json"
    done = read_json(out)
    if done and all(v == "ok" for v in done["sources"].values()) and not args.force:
        log.info("%s already complete; nothing to do", out.name)
        report.quiet = True
        return
    sv = cfg["surveillance"]
    client = client or NseClient({"maxRetries": sv["maxRetries"], "backoffSeconds": sv["backoffSeconds"]})
    data = surveil.normalise(sv["sources"], download(cfg, client, log, asof, folder), asof, now.isoformat(timespec="seconds"))
    ok = [k for k, v in data["sources"].items() if v == "ok"]
    n = surveil.counts(data)
    report.lines.append("Sources: " + ", ".join(f"{k} {v}" for k, v in data["sources"].items()))
    report.lines.append("Counts: " + ", ".join(f"{k} {v}" for k, v in n.items()))
    if not ok:
        report.error = "every NSE source failed; no list written"
        return
    write_json(out, data)
    report.partial = len(ok) < len(surveil.SOURCES)
    report.block.update(sources=data["sources"])
    report.lines.append(f"Normalised file written: {out.name}")


if __name__ == "__main__":
    run_stage("surveillance", run)
