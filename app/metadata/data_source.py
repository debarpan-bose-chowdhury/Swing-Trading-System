"""Data Source stage: fetch NSE equity list + Bhavcopy market caps, join, write raw snapshot."""

import csv
import io
import json
import logging
import time
import urllib.request
import zipfile
from datetime import date
from http.cookiejar import CookieJar
from pathlib import Path

from app.metadata.common import run_stage, write_rows

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,text/html,application/zip,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


class NseClient:
    """Cookie-carrying HTTP client with retry + exponential backoff."""

    def __init__(self, nse: dict, sleep=time.sleep) -> None:
        self.retries, self.backoff, self.sleep = nse["maxRetries"], nse["backoffSeconds"], sleep
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.opener.addheaders = list(HEADERS.items())

    def get(self, url: str) -> bytes:
        for attempt in range(self.retries):
            try:
                with self.opener.open(url, timeout=30) as resp:
                    return resp.read()
            except Exception:
                if attempt == self.retries - 1:
                    raise
                self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        raise RuntimeError("maxRetries must be >= 1")


def parse_listing_dates(content: bytes) -> dict[str, str]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    out = {}
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if row.get("SYMBOL") and row.get("DATE OF LISTING"):
            out[row["SYMBOL"]] = row["DATE OF LISTING"]
    return out


def find_bhavcopy_url(reports_json: bytes) -> str:
    data = json.loads(reports_json)
    items = data if isinstance(data, list) else [i for v in data.values() if isinstance(v, list) for i in v]
    for item in items:
        if isinstance(item, dict) and "bhavcopy (pr)(zip)" in str(item.get("displayName", "")).lower():
            path, name = item.get("filePath"), item.get("fileActlName")
            if not path or not name:
                raise ValueError("Bhavcopy (PR)(zip) entry lacks filePath/fileActlName")
            return f"{path.rstrip('/')}/{name}"
    raise ValueError("Bhavcopy (PR)(zip) entry not found in daily reports")


def parse_market_caps(zip_bytes: bytes, log: logging.Logger) -> dict[str, float]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = next((n for n in zf.namelist() if Path(n).name.lower().startswith("mcap")), None)
        if name is None:
            raise ValueError("No mcap file in Bhavcopy zip")
        text = zf.read(name).decode("utf-8-sig")
    out: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        cap_key = next((k for k in row if "market cap" in k), None)
        symbol = row.get("symbol")
        if not symbol or cap_key is None:
            continue
        try:
            out[symbol] = float(row[cap_key].replace(",", ""))
        except ValueError:
            log.warning("Bad market cap for %s: %r", symbol, row[cap_key])
    if not out:
        raise ValueError("mcap file has no usable symbol/market cap rows")
    return out


def join(listing: dict[str, str], caps: dict[str, float], log: logging.Logger) -> list[dict]:
    for sym in sorted(listing.keys() ^ caps.keys()):
        log.warning("Dropping %s: present in only one source", sym)
    return [
        {"Symbol": s, "MarketCap": caps[s], "InceptionDate": listing[s]}
        for s in sorted(listing.keys() & caps.keys())
    ]


def run(cfg: dict, today: date, log: logging.Logger, client: NseClient | None = None) -> Path:
    nse = cfg["nse"]
    client = client or NseClient(nse)
    client.get(nse["homeUrl"])  # pick up session cookies
    listing = parse_listing_dates(client.get(nse["equityListUrl"]))
    bhav_url = find_bhavcopy_url(client.get(nse["dailyReportsUrl"]))
    caps = parse_market_caps(client.get(bhav_url), log)
    rows = join(listing, caps, log)
    out = Path(cfg["paths"]["rawData"]) / f"{today}.csv"
    write_rows(out, rows)
    log.info("Wrote %d rows to %s", len(rows), out)
    return out


if __name__ == "__main__":
    run_stage("data_source", run)
