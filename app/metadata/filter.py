"""Filter stage: drop recent listings, bucket by market cap, keep top-N per bucket."""

import logging
from datetime import date
from pathlib import Path

from app.metadata.common import is_healthy, parse_date, read_rows, run_stage, write_rows


def run(cfg: dict, today: date, log: logging.Logger) -> list[Path]:
    if not is_healthy(cfg):
        log.warning("health.json is not healthy; running anyway so the pipeline can recover")
    raw = Path(cfg["paths"]["rawData"]) / f"{today}.csv"
    min_days = cfg["filter"]["minInceptionDays"]

    rows = []
    for row in read_rows(raw):
        listed = parse_date(row["InceptionDate"])
        try:
            cap = float(row["MarketCap"])
        except ValueError:
            cap = None
        if listed is None or cap is None:
            log.warning("Skipping %s: unparseable date or market cap", row["Symbol"])
        elif (today - listed).days >= min_days:
            rows.append({**row, "MarketCap": cap})

    written, upper = [], float("inf")
    for bucket in sorted(cfg["filter"]["capBuckets"], key=lambda b: -b["minMarketCap"]):
        members = [r for r in rows if bucket["minMarketCap"] <= r["MarketCap"] < upper]
        members.sort(key=lambda r: (-r["MarketCap"], r["Symbol"]))
        out = Path(cfg["paths"]["storage"]) / f"{bucket['name']}_{today}.csv"
        write_rows(out, members[: bucket["topN"]])
        written.append(out)
        upper = bucket["minMarketCap"]
        log.info("%s: %d eligible, kept %d", bucket["name"], len(members), min(len(members), bucket["topN"]))
    return written


if __name__ == "__main__":
    run_stage("filter", run)
