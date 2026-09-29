"""Notifier stage: verify today's files exist and write health.json."""

import json
import logging
from datetime import date, datetime
from pathlib import Path

from app.metadata.common import run_stage


def run(cfg: dict, today: date, log: logging.Logger) -> dict:
    raw, storage = Path(cfg["paths"]["rawData"]), Path(cfg["paths"]["storage"])
    expected = [raw / f"{today}.csv"] + [storage / f"{b['name']}_{today}.csv" for b in cfg["filter"]["capBuckets"]]
    missing = [str(p) for p in expected if not p.is_file()]
    health = {
        "status": "unhealthy" if missing else "healthy",
        "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "missing": missing,
    }
    path = Path(cfg["paths"]["health"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(health, indent=2), encoding="utf-8")
    if missing:
        log.error("Unhealthy, missing: %s", missing)
    else:
        # Delivery to Ticker Data System / Artifact Handler is out of scope; log only.
        log.info("Healthy: fresh data available for Ticker Data System and Artifact Handler")
    return health


if __name__ == "__main__":
    run_stage("notifier", run)
