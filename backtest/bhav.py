"""NSE bhavcopy layer: probe the two file formats, download and cache the daily files, normalise them, cross-check the Yahoo series.

  python -m backtest.bhav --check                     config only (no network, no writes)
  python -m backtest.bhav --probe                     download one file of each format, check the headers (network)
  python -m backtest.bhav --download [--from D --to D]  fill the raw cache for every trading day (network; resumable; needs a passed probe)
  python -m backtest.bhav --build                     parse the raw cache into backtest/data/bhav/bhav_<year>.parquet
  python -m backtest.bhav --crosscheck [--strict]     compare the Yahoo prices in app/data with bhavcopy -> backtest/data/bhav_crosscheck.csv
  python -m backtest.bhav --summary                   what the cross-check findings are made of (short text to read or paste)

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
OPTIONAL = ("isin",)  # early legacy files (2007) have no ISIN column; nothing downstream needs it
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


def to_iso(dates: pd.Series, fmt: str) -> pd.Series:
    """ISO dates from text. A file that writes the year with two digits (seen in 2020-07-13) is read with %y instead of %Y."""
    try:
        return pd.to_datetime(dates, format=fmt).dt.strftime("%Y-%m-%d")
    except ValueError:
        if "%Y" not in fmt:
            raise
        return pd.to_datetime(dates, format=fmt.replace("%Y", "%y")).dt.strftime("%Y-%m-%d")


def parse(text: str, fmt: dict, keep: list[str], day: str | None = None) -> pd.DataFrame:
    """Normalised rows (NORMAL columns) of the kept series. Raises ValueError naming every column the mapping expects but the file lacks."""
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    want = {**{k: v for k, v in fmt["columns"].items() if k not in OPTIONAL}, "date": fmt["dateColumn"]}
    missing = [f"{k}={v}" for k, v in want.items() if v not in df.columns]
    if missing:
        raise ValueError(f"columns not found in the file: {', '.join(missing)} (file has {list(df.columns)})")
    c = fmt["columns"]
    df = df[df[c["series"]].str.strip().isin(keep)]
    df = df.assign(_rank=df[c["series"]].str.strip().map({k: i for i, k in enumerate(keep)})).sort_values("_rank", kind="stable")
    df = df.drop_duplicates([c["symbol"], fmt["dateColumn"]], keep="first")  # one row per symbol and day; the earlier series in `keep` wins
    out = pd.DataFrame({
        "Ticker": df[c["symbol"]].str.strip(), "Date": to_iso(df[fmt["dateColumn"]].str.strip(), fmt["dateFormat"]),
        "Series": df[c["series"]].str.strip(), "Isin": df[c["isin"]].str.strip() if c["isin"] in df.columns else ""})
    for k, name in (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("prevClose", "PrevClose"), ("volume", "Volume"), ("value", "Value")):
        out[name] = pd.to_numeric(df[c[k]].str.replace(",", ""), errors="coerce")
    if day is not None and len(out) and set(out.Date) != {day}:
        raise ValueError(f"file for {day} holds dates {sorted(set(out.Date))[:3]}")
    return out[NORMAL].reset_index(drop=True)


# --- probe -------------------------------------------------------------------------------------------------------
def probe(cfg: dict, client: BhavClient) -> dict:
    """Fetch the sample days of each format (spread over its years); record whether each downloads and parses with the mapping."""
    out = {}
    samples = folder(cfg) / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    for name, days in cfg["bhav"]["probeDays"].items():
        fmt, results = cfg["bhav"]["formats"][name], []
        for day in days:
            res = {"day": day, "url": fmt["url"].format(yyyy=day[:4], dd=day[8:], MON=MONTHS[int(day[5:7]) - 1], yyyymmdd=day.replace("-", ""))}
            try:
                text = unzip(client.get(res["url"]))
                (samples / f"{name}_{day}.csv").write_text(text, encoding="utf-8")
                res["header"] = text.splitlines()[0] if text else ""
                rows = parse(text, fmt, cfg["bhav"]["seriesKeep"], day)
                res |= {"ok": len(rows) > 100, "rows": len(rows), "sample": rows.head(2).to_dict("records")}
                if not res["ok"]:
                    res["error"] = f"only {len(rows)} kept rows"
            except Exception as e:
                res |= {"ok": False, "error": repr(e)}
            results.append(res)
        out[name] = {"ok": all(r["ok"] for r in results), "days": results}
    atomic(folder(cfg) / "probe.json", lambda tmp: tmp.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8"))
    return out


def probe_passed(cfg: dict) -> bool:
    path = folder(cfg) / "probe.json"
    if not path.exists():
        return False
    got = json.loads(path.read_text(encoding="utf-8"))
    want = cfg["bhav"]["probeDays"]
    return all(got.get(n, {}).get("ok") and [r["day"] for r in got[n]["days"]] == want[n] for n in cfg["bhav"]["formats"])


# --- download and build ------------------------------------------------------------------------------------------
def download(cfg: dict, client: BhavClient, cal: Calendar, start: str, end: str, sleep=time.sleep) -> dict:
    """Cache the raw CSV of every trading day in [start, end] not cached yet. A 404 is remembered as a .missing marker."""
    if not probe_passed(cfg):
        raise prep.MissingInput("the bhavcopy probe has not passed for both formats: run `python -m backtest.bhav --probe` and fix the mapping first")
    raw = folder(cfg) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    client.get(cfg["bhav"]["client"]["homeUrl"])  # session cookies
    days = [d.isoformat() for d in cal.days(date.fromisoformat(start), date.fromisoformat(end))]
    done = {"fetched": 0, "cached": 0, "missing": 0, "failed": [], "aborted": False}
    streak, limit = 0, cfg["bhav"]["client"]["abortAfterFailures"]
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
            streak = 0
        except NotFound:
            (raw / f"{day}.missing").write_text("404", encoding="utf-8")
            done["missing"] += 1
        except Exception as e:
            log.warning("%s: %r", day, e)
            done["failed"].append(day)
            streak += 1
            if streak >= limit:  # a format drift or a block, not a one-off: stop before hammering NSE for thousands of days
                done["aborted"] = True
                log.error("%d failures in a row (first: %s); stopping. Fix the cause (see the message above), then re-run: cached days are kept", limit, done["failed"][-limit])
                return done
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
            rows.append((t, d[i], "PRICE_SPIKE", float(r[i] / prev[i] - 1), f"yahoo {yb.Close.loc[d[i]]:.2f} vs bhav {b[t].Close.loc[d[i]]:.2f}"))
        for i in np.flatnonzero(step):
            rows.append((t, d[i], "RATIO_BREAK", float(r[i] / prev[i]), f"yahoo/bhav ratio {prev[i]:.4f} -> {r[i]:.4f}"))
        vr = yb.Volume.loc[both].to_numpy(float) * r
        bv = b[t].Volume.loc[both].to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            off = np.abs(vr / bv - 1) > tol["volumeTolerance"]
        for i in np.flatnonzero(off & (bv > 0)):
            rows.append((t, d[i], "VOLUME_MISMATCH", float(vr[i] / bv[i] - 1), "report only"))
    return pd.DataFrame(rows, columns=["Ticker", "Date", "Kind", "Value", "Detail"]).sort_values(["Ticker", "Date", "Kind"], ignore_index=True)


# --- summary of the cross-check ----------------------------------------------------------------------------------
NICE_RATIOS = (1.25, 4 / 3, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 50, 100)  # usual split / bonus factors


def series_of(cfg: dict, day: str, ticker: str, cache: dict) -> list[str]:
    """Series the ticker traded in on `day` according to the raw file (all series, not only the kept ones)."""
    if day not in cache:
        path = folder(cfg) / "raw" / f"{day}.csv"
        cache[day] = pd.read_csv(io.StringIO(path.read_text(encoding="utf-8")), dtype=str, keep_default_na=False) if path.exists() else None
    df = cache[day]
    if df is None:
        return ["(no raw file)"]
    c = cfg["bhav"]["formats"][format_for(cfg, day)]["columns"]
    hit = df[df[c["symbol"]].str.strip() == ticker]
    return sorted(set(hit[c["series"]].str.strip())) or ["(symbol absent)"]


def counts(s: pd.Series, top: int | None = None) -> dict:
    """value_counts as a plain dict of ints, for printing."""
    return {k: int(v) for k, v in (s if top is None else s.head(top)).items()}


SMALL_STEP = (1.003, 1.08)  # step sizes that dividends or rights-issue adjustments produce, not splits


def adjustment_basis(cfg: dict, out: pd.DataFrame, data) -> list[str]:
    """Is the stored Yahoo Close price-only or already dividend-adjusted? Compare small ratio breaks with Yahoo dividend ex-dates and
    with steps in AdjClose/Close, and look at how far AdjClose sits from Close at the start of each series."""
    lines = []
    first = pd.Series({t: float(df.AdjClose.iloc[0] / df.Close.iloc[0]) for t, df in data.series.items() if len(df)})
    lines.append(f"AdjClose/Close at each series' first row: below 0.99 for {int((first < 0.99).sum())} of {len(first)} tickers, median {first.median():.3f}"
                 " (a price-only Close gives values below 1 for dividend payers; all 1.000 would mean Close is already dividend-adjusted)")
    g = out[out.Kind == "RATIO_BREAK"]
    f = g.Value.where(g.Value >= 1, 1 / g.Value)
    small = g[(f > SMALL_STEP[0]) & (f < SMALL_STEP[1])]
    if small.empty:
        return lines
    path = Path(cfg["paths"]["data"]) / "dividends.csv"
    ex = set(zip(*[pd.read_csv(path, dtype=str)[c] for c in ("Ticker", "ExDate")])) if path.exists() else None
    adj_step = 0
    for r in small.itertuples():
        df = data.series.get(r.Ticker)
        if df is None:
            continue
        i = int(np.searchsorted(df.Date.to_numpy(), r.Date))
        if 0 < i < len(df) and i < len(df) and df.Date.iloc[i] == r.Date:
            a = df.AdjClose.to_numpy(float) / df.Close.to_numpy(float)
            adj_step += abs(a[i] / a[i - 1] - 1) > 0.002
    lines.append(f"small ratio breaks (1-8%): {len(small)}; coincide with an AdjClose/Close step in the stored data: {int(adj_step)}; "
                 + (f"coincide with a Yahoo dividend ex-date: {sum((r.Ticker, r.Date) in ex for r in small.itertuples())}" if ex is not None else "dividends.csv not found (run prep --dividends)"))
    return lines


def summarize(cfg: dict, out: pd.DataFrame, top: int = 8, data=None) -> str:
    """A compact text report of bhav_crosscheck.csv: what the findings are made of."""
    lines = [f"findings {counts(out.Kind.value_counts())} over {out.Ticker.nunique()} tickers"]
    out = out.assign(Year=out.Date.str[:4])
    lines.append("by year: " + "; ".join(f"{k} " + ",".join(f"{y}:{n}" for y, n in g.Year.value_counts().sort_index().items()) for k, g in out.groupby("Kind")))
    cache: dict = {}
    for kind in ("NO_BHAV_ROW", "NO_YAHOO_ROW"):
        g = out[out.Kind == kind]
        if g.empty:
            continue
        per_day = g.Date.value_counts()
        lines.append(f"{kind}: {len(g)} rows; dates with 10+ tickers (whole-market gaps): {counts(per_day[per_day >= 10], top)}; top tickers {counts(g.Ticker.value_counts(), top)}")
        if kind == "NO_BHAV_ROW":
            found = pd.Series([",".join(series_of(cfg, d, t, cache)) for t, d in zip(g.Ticker, g.Date, strict=True)]).value_counts()
            lines.append(f"  series the missing ticker-days traded in per the raw files: {counts(found, top)}")
    g = out[out.Kind == "PRICE_SPIKE"]
    if len(g):
        per_day = g.Date.value_counts()
        lines.append(f"PRICE_SPIKE: {len(g)}; |departure| quantiles 50/90/99% = {[round(float(g.Value.abs().quantile(q)), 3) for q in (.5, .9, .99)]}; "
                     f"dates with 5+ tickers {counts(per_day[per_day >= 5], top)}; top tickers {counts(g.Ticker.value_counts(), top)}")
        a = g.Value.abs()
        lines.append(f"  by size: <1% {int((a < .01).sum())}, 1-2% {int(((a >= .01) & (a < .02)).sum())}, 2-5% {int(((a >= .02) & (a < .05)).sum())}, "
                     f"5-20% {int(((a >= .05) & (a < .2)).sum())}, 20%+ {int((a >= .2).sum())}")
        lines.append("  largest: " + " | ".join(f"{r.Ticker} {r.Date} {r.Detail}" for r in g.reindex(g.Value.abs().sort_values(ascending=False).index).head(5).itertuples()))
    g = out[out.Kind == "RATIO_BREAK"]
    if len(g):
        f = g.Value.where(g.Value >= 1, 1 / g.Value)
        nice = f.apply(lambda v: any(abs(v / n - 1) < 0.015 for n in NICE_RATIOS))
        lines.append(f"RATIO_BREAK: {len(g)}; {int(nice.sum())} look like split/bonus factors, {int((~nice).sum())} do not; most common step sizes {counts(f.round(2).value_counts(), top)}")
        odd = g[~nice]
        lines.append("  not split-like (first 8): " + " | ".join(f"{r.Ticker} {r.Date} {r.Detail}" for r in odd.head(8).itertuples()))
        if data is not None:
            lines += adjustment_basis(cfg, out, data)
    g = out[out.Kind == "VOLUME_MISMATCH"]
    if len(g):
        lines.append(f"VOLUME_MISMATCH (report only): {len(g)}; median |difference| {float(g.Value.abs().median()):.2f}; top tickers {counts(g.Ticker.value_counts(), top)}")
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------------------------------------------
def client_for(cfg: dict) -> BhavClient:
    c = cfg["bhav"]["client"]
    return BhavClient({"maxRetries": c["maxRetries"], "backoffSeconds": c["backoffSeconds"]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.bhav")
    g = parser.add_mutually_exclusive_group(required=True)
    for flag in ("check", "probe", "download", "build", "crosscheck", "summary"):
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
                for d in r["days"]:
                    print(f"{name} {d['day']}: {'OK' if d['ok'] else 'FAILED'} {d.get('error', '')}\n  header: {d.get('header', '')[:160]}")
            return 0 if probe_passed(cfg) else 3
        if args.download:
            cal = Calendar(Path(cfg["paths"]["appConfig"]) / "nse_calendar.json")
            done = download(cfg, client_for(cfg), cal, args.start or cfg["bhav"]["from"], args.end or (date.today() - timedelta(days=1)).isoformat())
            print(f"bhav: {done['fetched']} fetched, {done['cached']} cached, {done['missing']} missing, {len(done['failed'])} failed {done['failed'][:5]}"
                  + (" -- ABORTED after repeated failures" if done["aborted"] else ""))
            return 1 if done["failed"] else 0
        if args.build:
            print(f"bhav: built {build(cfg)}")
            return 0
        if args.summary:
            path = Path(cfg["paths"]["data"]) / "bhav_crosscheck.csv"
            if not path.exists():
                raise prep.MissingInput("no bhav_crosscheck.csv: run --crosscheck first")
            print(summarize(cfg, pd.read_csv(path, dtype={"Ticker": str, "Date": str, "Detail": str}, keep_default_na=False), data=prep.load_pit(cfg)))
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
