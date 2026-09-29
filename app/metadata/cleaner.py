"""Cleaner stage: quarterly sweep keeping only the newest dated file per store/bucket."""

import logging
from datetime import date
from pathlib import Path

from app.metadata.common import is_healthy, run_stage

DATE_GLOB = "????-??-??"


def sweep(directory: Path, prefix: str, log: logging.Logger) -> None:
    files = sorted(directory.glob(f"{prefix}{DATE_GLOB}.csv"), key=lambda p: p.stem)  # ISO date sorts
    for old in files[:-1]:
        old.unlink()
        log.info("Deleted %s", old)


def run(cfg: dict, today: date, log: logging.Logger) -> None:
    if not is_healthy(cfg):
        log.warning("health.json is not healthy; skipping cleanup")
        return
    for bucket in cfg["filter"]["capBuckets"]:
        sweep(Path(cfg["paths"]["storage"]), f"{bucket['name']}_", log)
    sweep(Path(cfg["paths"]["rawData"]), "", log)


if __name__ == "__main__":
    run_stage("cleaner", run)
