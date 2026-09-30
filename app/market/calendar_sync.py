"""Keep nse_calendar.json current: NSE holiday API (CM segment) plus special sessions and old holidays from index rows.

Layers, weakest to strongest:
  * NSE holiday-master API, year by year: holidays; a description ending in '*' is a special (Muhurat) session.
  * Index rows (Yahoo): a weekend date with an index row is a special session; before the API's first year,
    a weekday that no index traded is a holiday.
Known dates are never dropped; only a date newly seen as special leaves the holidays.
Any failure is a note, and the stored calendar stays in use.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from app.market.common import Report, atomic, iso, shift
from app.market.fetcher import Fetcher
from app.metadata.data_source import NseClient


def parse_year(content: bytes) -> tuple[set[str], set[str]]:
    """(holidays, special sessions) from one holiday-master response, CM segment only."""
    holidays, special = set(), set()
    for item in json.loads(content).get("CM", []):
        day = iso(datetime.strptime(item["tradingDate"], "%d-%b-%Y").date())
        (special if item.get("description", "").strip().endswith("*") else holidays).add(day)
    return holidays, special


def index_special_sessions(rows: pd.DataFrame) -> set[str]:
    """Weekend dates on which an index traded."""
    days = pd.to_datetime(rows.Date.drop_duplicates(), format="%Y-%m-%d")
    return set(days[days.dt.weekday >= 5].dt.strftime("%Y-%m-%d"))


def index_holidays(rows: pd.DataFrame, before: date) -> set[str]:
    """Weekdays before `before`, within the indices' history, on which no index has a row."""
    if rows.empty:
        return set()
    traded = set(rows.Date)
    span = pd.bdate_range(rows.Date.min(), pd.Timestamp(before) - pd.Timedelta(days=1))
    return {d for d in span.strftime("%Y-%m-%d") if d not in traded}


def merge(current: dict, holidays: set[str], special: set[str]) -> dict:
    special = set(current.get("specialSessions", [])) | special
    holidays = (set(current.get("holidays", [])) | holidays) - special
    return {"holidays": sorted(holidays), "specialSessions": sorted(special)}


def sync(cfg: dict, fx: Fetcher, log: logging.Logger, report: Report, today: date, full: bool, client: NseClient | None = None) -> None:
    """Refresh the calendar file. full: every year since firstYear plus index-derived history (Migrator);
    otherwise the current and next year plus the lookback window's weekend sessions (Updator).
    """
    conf = cfg.get("calendarSync")
    if conf is None:
        return
    path = Path(cfg["paths"]["calendar"])
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    holidays, special, first_year = set(), set(), None
    client = client or NseClient(conf)
    years = range(conf["firstYear"] if full else today.year, today.year + 2)
    try:
        client.get(conf["homeUrl"])  # session cookies
    except Exception as e:
        log.warning("NSE holiday API unreachable: %r", e)
        report.notes.append(f"calendar: NSE holiday API unreachable ({e!r}); stored calendar used")
        years = range(0)
    for year in years:
        try:
            h, s = parse_year(client.get(conf["holidayUrl"].format(year=year)))
        except Exception as e:
            log.warning("NSE holidays for %d failed: %r", year, e)
            report.notes.append(f"calendar: NSE holidays for {year} unavailable ({e!r})")
            continue
        if h or s:
            first_year = min(first_year or year, year)
            holidays |= h
            special |= s

    indices = json.loads(Path(cfg["paths"]["indices"]).read_text(encoding="utf-8"))["indices"]
    kw = {"period": "max"} if full else {"start": shift(iso(today), -cfg["updator"]["lookbackDays"]), "end": shift(iso(today), 1)}
    frame = fx.fetch(indices, **kw) if indices else None
    if frame is None:
        report.notes.append("calendar: index history unavailable; special sessions and old holidays not derived")
    else:
        special |= index_special_sessions(frame)
        if full and first_year:
            holidays |= index_holidays(frame, date(first_year, 1, 1))

    merged = merge(current, holidays, special)
    if merged != {k: sorted(current.get(k, [])) for k in ("holidays", "specialSessions")}:
        atomic(path, lambda tmp: tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8"))
        log.info("Calendar updated: %d holidays, %d special sessions", len(merged["holidays"]), len(merged["specialSessions"]))
