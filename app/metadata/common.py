"""Shared helpers for the ticker-metadata pipeline stages."""

import csv
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

CONFIG_PATH = "app/config/config.json"
ROW_FIELDS = ["Symbol", "MarketCap", "InceptionDate"]
DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d")


def load_config() -> dict:
    return json.loads(Path(os.environ.get("CONFIG_PATH", CONFIG_PATH)).read_text(encoding="utf-8"))


def parse_date(text: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_healthy(cfg: dict) -> bool:
    """Missing health.json counts as healthy (first run); unreadable or non-healthy blocks."""
    path = Path(cfg["paths"]["health"])
    if not path.exists():
        return True
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("status") == "healthy"
    except (ValueError, AttributeError):
        return False


def setup_logging(stage: str, cfg: dict, today: date) -> logging.Logger:
    log_file = Path(cfg["paths"]["health"]).parent / "logs" / f"{stage}_{today}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(stage)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


def check_stage(stage: str) -> int:
    """`--check`: confirm the config loads, with no network access, log file or data writes.

    Importing the stage module (which `python -m` already did) proves its dependencies are installed.
    """
    try:
        cfg = load_config()
        missing = [k for k in ("rawData", "storage", "health") if k not in cfg["paths"]]
        if missing:
            raise KeyError(f"paths missing from the config: {', '.join(missing)}")
    except Exception as e:
        print(f"{stage}: check FAILED: {e!r}", file=sys.stderr)
        return 1
    print(f"{stage}: check ok")
    return 0


def run_stage(stage: str, run, argv: list[str] | None = None) -> None:
    """Common entry point: load config, log to file+stdout, exit non-zero on failure.

    With `--check` on the command line the stage only validates its environment (see check_stage).
    """
    if "--check" in (sys.argv[1:] if argv is None else argv):
        sys.exit(check_stage(stage))

    cfg, today = load_config(), date.today()
    log = setup_logging(stage, cfg, today)
    try:
        run(cfg, today, log)
    except Exception:
        log.exception("%s failed", stage)
        sys.exit(1)
