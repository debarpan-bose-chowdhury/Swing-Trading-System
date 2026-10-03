"""NSE bhavcopy layer: probe the two file formats, download and cache the daily files, normalise them, cross-check the Yahoo series.

  python -m backtest.bhav --check                     config only (no network, no writes)
  python -m backtest.bhav --probe                     download one file of each format, check the headers (network)
  python -m backtest.bhav --download [--from D --to D]  fill the raw cache for every trading day (network; resumable; needs a passed probe)
  python -m backtest.bhav --build                     parse the raw cache into backtest/data/bhav/bhav_<year>.parquet
  python -m backtest.bhav --crosscheck [--strict]     compare the Yahoo prices in app/data with bhavcopy -> backtest/data/bhav_crosscheck.csv

The URL templates and column names are unverified until --probe passes on your PC (the NSE hosts are not reachable from the cloud).
Reads app/data read-only; every output is under backtest/data/. Exit codes: 0 ok, 1 failed, 3 a gate is not met (probe not passed,
or --strict found price mismatches).
"""

import argparse
import io
import json
import logging
import random
import sys
import time
import urllib.error
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.market.common import atomic, write_csv
from app.market.tradingcal import Calendar
from app.metadata.data_source import NseClient
from backtest import config, prep

log = logging.getLogger("backtest.bhav")
NORMAL = ["Ticker", "Date", "Series", "Open", "High", "Low", "Close", "PrevClose", "Volume", "Value", "Isin"]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


class NotFound(Exception):
    """NSE has no file for that day (a holiday the calendar does not know, or a day before the archive starts)."""


class BhavClient(NseClient):
    """NseClient that treats HTTP 404 as 'no file' instead of retrying it."""

    def get(self, url: str) -> bytes:
        for attempt in range(self.retries):
            try:
                with self.opener.open(url, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(url) from e
                if attempt == self.retries - 1:
                    raise
                self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            except Exception:
                if attempt == self.retries - 1:
                    raise
                self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        raise RuntimeError("maxRetries must be >= 1")


def folder(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"]) / "bhav"


def format_for(cfg: dict, day: str) -> str:
    f = cfg["bhav"]["formats"]
    return "udiff" if day >= f["udiff"]["from"] else "legacy"


def url_for(cfg: dict, day: str) -> str:
    d = date.fromisoformat(day)
    return cfg["bhav"]["formats"][format_for(cfg, day)]["url"].format(yyyy=d.year, dd=f"{d.day:02d}", MON=MONTHS[d.month - 1], yyyymmdd=d.strftime("%Y%m%d"))


def unzip(payload: bytes) -> str:
    """The CSV text inside a bhavcopy zip (or the text itself when the payload is not a zip)."""
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            return z.read(z.namelist()[0]).decode("utf-8-sig")
    return payload.decode("utf-8-sig")


def parse(text: str, fmt: dict, keep: list[str], day: str | None = None) -> pd.DataFrame:
    """Normalised rows (NORMAL columns) of the kept series. Raises ValueError naming every column the mapping expects but the file lacks."""
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    want = {**fmt["columns"], "date": fmt["dateColumn"]}
    missing = [f"{k}={v}" for k, v in want.items() if v not in df.columns]
    if missing:
        raise ValueError(f"columns not found in the file: {', '.join(missing)} (file has {list(df.columns)})")
    c = fmt["columns"]
    df = df[df[c["series"]].str.strip().isin(keep)]
    out = pd.DataFrame({
        "Ticker": df[c["symbol"]].str.strip(), "Date": pd.to_datetime(df[fmt["dateColumn"]].str.strip(), format=fmt["dateFormat"]).dt.strftime("%Y-%m-%d"),
        "Series": df[c["series"]].str.strip(), "Isin": df[c["isin"]].str.strip()})
    for k, name in (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("prevClose", "PrevClose"), ("volume", "Volume"), ("value", "Value")):
        out[name] = pd.to_numeric(df[c[k]].str.replace(",", ""), errors="coerce")
    if day is not None and len(out) and set(out.Date) != {day}:
        raise ValueError(f"file for {day} holds dates {sorted(set(out.Date))[:3]}")
    return out[NORMAL].reset_index(drop=True)


# --- probe -------------------------------------------------------------------------------------------------------
def probe(cfg: dict, client: BhavClient) -> dict:
    """Fetch one sample day per format; record whether it downloads and parses with the configured mapping."""
    out = {}
    samples = folder(cfg) / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    for name, day in cfg["bhav"]["probeDays"].items():
        fmt = cfg["bhav"]["formats"][name]
        res = {"day": day, "url": fmt["url"].format(yyyy=day[:4], dd=day[8:], MON=MONTHS[int(day[5:7]) - 1], yyyymmdd=day.replace("-", ""))}
        try:
            text = unzip(client.get(res["url"]))
            (samples / f"{name}_{day}.csv").write_text(text, encoding="utf-8")
            res["header"] = text.splitlines()[0] if text else ""
            rows = parse(text, fmt, cfg["bhav"]["seriesKeep"], day)
            res |= {"ok": len(rows) > 100, "rows": len(rows), "sample": rows.head(3).to_dict("records")}
            if not res["ok"]:
                res["error"] = f"only {len(rows)} kept rows"
        except Exception as e:
            res |= {"ok": False, "error": repr(e)}
        out[name] = res
    atomic(folder(cfg) / "probe.json", lambda tmp: tmp.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8"))
    return out


def probe_passed(cfg: dict) -> bool:
    path = folder(cfg) / "probe.json"
    if not path.exists():
        return False
    got = json.loads(path.read_text(encoding="utf-8"))
    return all(got.get(n, {}).get("ok") for n in cfg["bhav"]["formats"]) and all(got[n]["day"] == cfg["bhav"]["probeDays"][n] for n in got)


# --- download and build ------------------------------------------------------------------------------------------
def download(cfg: dict, client: BhavClient, cal: Calendar, start: str, end: str, sleep=time.sleep) -> dict:
    """Cache the raw CSV of every trading day in [start, end] not cached yet. A 404 is remembered as a .missing marker."""
    if not probe_passed(cfg):
        raise prep.MissingInput("the bhavcopy probe has not passed for both formats: run `python -m backtest.bhav --probe` and fix the mapping first")
    raw = folder(cfg) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    client.get(cfg["bhav"]["client"]["homeUrl"])  # session cookies
    days = [d.isoformat() for d in cal.days(date.fromisoformat(start), date.fromisoformat(end))]
    done = {"fetched": 0, "cached": 0, "missing": 0, "failed": []}
    for day in days:
        if (raw / f"{day}.csv").exists():
            done["cached"] += 1
            continue
        if (raw / f"{day}.missing").exists():
            done["missing"] += 1
            continue
        try:
            text = unzip(client.get(url_for(cfg, day)))
            parse(text, cfg["bhav"]["formats"][format_for(cfg, day)], cfg["bhav"]["seriesKeep"], day)  # never cache a file we cannot read
            atomic(raw / f"{day}.csv", lambda tmp, t=text: tmp.write_text(t, encoding="utf-8"))
            done["fetched"] += 1
        except NotFound:
            (raw / f"{day}.missing").write_text("404", encoding="utf-8")
            done["missing"] += 1
        except Exception as e:
            log.warning("%s: %r", day, e)
            done["failed"].append(day)
        sleep(cfg["bhav"]["client"]["gapSeconds"])
    return done


def build(cfg: dict) -> dict:
    """Parse the raw cache into one Parquet file per year. Returns rows per year."""
    raw, out = folder(cfg) / "raw", folder(cfg)
    by_year: dict[str, list[pd.DataFrame]] = {}
    for f in sorted(raw.glob("????-??-??.csv")):
        by_year.setdefault(f.stem[:4], []).append(parse(f.read_text(encoding="utf-8"), cfg["bhav"]["formats"][format_for(cfg, f.stem)], cfg["bhav"]["seriesKeep"], f.stem))
    counts = {}
    for year, frames in by_year.items():
        df = pd.concat(frames, ignore_index=True).sort_values(["Ticker", "Date"], ignore_index=True)
        atomic(out / f"bhav_{year}.parquet", lambda tmp, d=df: d.to_parquet(tmp, index=False))
        counts[year] = len(df)
    return counts


def load(cfg: dict, tickers: set[str] | None = None) -> pd.DataFrame:
    files = sorted(folder(cfg).glob("bhav_????.parquet"))
    if not files:
        raise prep.MissingInput("no bhav_<year>.parquet files: run --download then --build")
    filters = [("Ticker", "in", sorted(tickers))] if tickers else None
    return pd.concat([pd.read_parquet(f, filters=filters) for f in files], ignore_index=True)


# --- cross-check -------------------------------------------------------------------------------------------------
def crosscheck(series: dict[str, pd.DataFrame], bhav: pd.DataFrame, tol: dict) -> pd.DataFrame:
    """Yahoo (split-adjusted) against bhavcopy (raw), per ticker over every common date.

    ratio = Yahoo close / bhavcopy close is constant between corporate actions. A one-day departure that comes back is a bad bar
    (PRICE_SPIKE); a lasting step is a split or bonus the Yahoo series has adjusted (RATIO_BREAK, value = the step) or a break in
    the Yahoo series; dates present on one side only inside the common span are NO_BHAV_ROW / NO_YAHOO_ROW. VOLUME_MISMATCH
    (report only) is a day where Yahoo volume x ratio differs from bhavcopy volume by more than the volume tolerance.
    """
    rows = []
    b = {t: g.set_index("Date") for t, g in bhav.groupby("Ticker")}
    for t, y in series.items():
        if t not in b:
            continue
        yb = y.set_index("Date")
        both = yb.index.intersection(b[t].index)
        if len(both) < 3:
            continue
        lo, hi = both.min(), both.max()
        for d in sorted(set(yb.index[(yb.index >= lo) & (yb.index <= hi)]) - set(b[t].index)):
            rows.append((t, d, "NO_BHAV_ROW", 0.0, ""))
        for d in sorted(set(b[t].index[(b[t].index >= lo) & (b[t].index <= hi)]) - set(yb.index)):
            rows.append((t, d, "NO_YAHOO_ROW", 0.0, ""))
        r = yb.Close.loc[both].to_numpy(float) / b[t].Close.loc[both].to_numpy(float)
        d = np.array(both)
        prev, nxt = np.r_[np.nan, r[:-1]], np.r_[r[1:], np.nan]
        jump_in, jump_out = np.abs(r / prev - 1) > tol["closeTolerance"], np.abs(r / nxt - 1) > tol["closeTolerance"]
        bridge = np.abs(prev / nxt - 1) <= tol["closeTolerance"]
        spike = jump_in & jump_out & bridge
        step = jump_in & ~spike & ~np.r_[False, spike[:-1]]  # the day after a spike is the return to normal, not a step
        for i in np.flatnonzero(spike):
            rows.append((t, d[i], "PRICE_SPIKE", float(r[i] / prev[i] - 1), "ratio departs for one day"))
        for i in np.flatnonzero(step):
            rows.append((t, d[i], "RATIO_BREAK", float(r[i] / prev[i]), "lasting step: split/bonus or an adjustment break"))
        vr = yb.Volume.loc[both].to_numpy(float) * r
        bv = b[t].Volume.loc[both].to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            off = np.abs(vr / bv - 1) > tol["volumeTolerance"]
        for i in np.flatnonzero(off & (bv > 0)):
            rows.append((t, d[i], "VOLUME_MISMATCH", float(vr[i] / bv[i] - 1), "report only"))
    return pd.DataFrame(rows, columns=["Ticker", "Date", "Kind", "Value", "Detail"]).sort_values(["Ticker", "Date", "Kind"], ignore_index=True)


# --- CLI ---------------------------------------------------------------------------------------------------------
def client_for(cfg: dict) -> BhavClient:
    c = cfg["bhav"]["client"]
    return BhavClient({"maxRetries": c["maxRetries"], "backoffSeconds": c["backoffSeconds"]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.bhav")
    g = parser.add_mutually_exclusive_group(required=True)
    for flag in ("check", "probe", "download", "build", "crosscheck"):
        g.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--strict", action="store_true", help="with --crosscheck: exit 3 when any PRICE_SPIKE is found")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = config.load()
        if args.check:
            print("bhav: check ok" + ("" if probe_passed(cfg) else " (probe not passed yet: run --probe on a machine that can reach NSE)"))
            return 0
        if args.probe:
            res = probe(cfg, client_for(cfg))
            for name, r in res.items():
                print(f"{name} {r['day']}: {'OK' if r['ok'] else 'FAILED'} {r.get('error', '')}\n  header: {r.get('header', '')[:200]}")
            return 0 if probe_passed(cfg) else 3
        if args.download:
            cal = Calendar(Path(cfg["paths"]["appConfig"]) / "nse_calendar.json")
            done = download(cfg, client_for(cfg), cal, args.start or cfg["bhav"]["from"], args.end or (date.today() - timedelta(days=1)).isoformat())
            print(f"bhav: {done['fetched']} fetched, {done['cached']} cached, {done['missing']} missing, {len(done['failed'])} failed {done['failed'][:5]}")
            return 1 if done["failed"] else 0
        if args.build:
            print(f"bhav: built {build(cfg)}")
            return 0
        data = prep.load_pit(cfg)
        out = crosscheck(data.series, load(cfg, set(data.series)), cfg["bhav"]["crosscheck"])
        write_csv(out, Path(cfg["paths"]["data"]) / "bhav_crosscheck.csv")
        counts = out.Kind.value_counts().to_dict()
        print(f"bhav: crosscheck {counts}")
        return 3 if args.strict and counts.get("PRICE_SPIKE") else 0
    except prep.MissingInput as e:
        print(f"bhav: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"bhav: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
