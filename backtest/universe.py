"""Point-in-time universe tooling (Phase 7b). Run from the repo root.

  python -m backtest.universe --links                 build symbol_links.csv (ISIN chain + manual + NSE) and symbol_review.csv
  python -m backtest.universe --probe-symbolchange    download NSE's symbol-change file once and print its header (network)
  python -m backtest.universe --validate-adjust       derive split/dividend-adjusted series from the bhavcopy for today's names and compare with Yahoo

Needs the bhavcopy Parquet files (backtest.bhav --download --build). Outputs go under backtest/data/. Exit codes: 0 ok, 1 failed,
3 an input is missing.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from app.market.common import atomic, write_csv
from backtest import adjust, bhav, config, links, prep


def data_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"])


def build_links(cfg: dict, rows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(links, review list) from the bhavcopy rows (Ticker, Date, Isin, Value) and the configured sources."""
    u = cfg["universe"]
    sc = u["symbolChange"]
    table = links.combine(links.manual_links(Path(u["symbolMap"])), links.nse_links(data_dir(cfg) / "bhav" / "samples" / "symbolchange.csv", sc["layout"]),
                          links.isin_links(rows))
    return table, links.review_list(rows, table, rows.Date.max(), exclude=u["excludePattern"])


def run_links(cfg: dict) -> str:
    rows = bhav.load(cfg)[["Ticker", "Date", "Isin", "Value"]]
    table, review = build_links(cfg, rows)
    write_csv(table, data_dir(cfg) / "symbol_links.csv")
    write_csv(review, data_dir(cfg) / "symbol_review.csv")
    by = table.Source.value_counts().to_dict()
    lines = [f"{len(table)} symbol links {dict(by)}",
             f"{len(review)} stopped symbols with no link, biggest by traded value (candidates for backtest/config/symbol_map.csv, or real deaths):"]
    lines += [f"  {r.Symbol} last {r.LastDate} median value {r.MedianValue / 1e7:,.1f} Cr" for r in review.head(15).itertuples()]
    return "\n".join(lines)


def probe_symbolchange(cfg: dict, client) -> str:
    sc = cfg["universe"]["symbolChange"]
    text = bhav.unzip(client.get(sc["url"]))
    path = data_dir(cfg) / "bhav" / "samples" / "symbolchange.csv"
    atomic(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))
    got = links.nse_links(path, sc["layout"])
    return (f"saved {path} ({len(text.splitlines())} lines); first lines as they are:\n" + "\n".join(text.splitlines()[:3])
            + f"\nparsed with the configured layout: {len(got)} links, e.g. " + "; ".join(f"{r.Old}->{r.New} {r.Date}" for r in got.head(3).itertuples()))


def validate_adjust(cfg: dict, yahoo: dict[str, pd.DataFrame], raw: pd.DataFrame, table: pd.DataFrame, clean_from: str = "2012-01-01") -> str:
    """Derive adjusted series from the raw bhavcopy for names Yahoo also has, and compare. Dates before clean_from are skipped (Yahoo's 2007-09 closes are noisy)."""
    tol = cfg["universe"]["adjust"]
    final = links.resolve(table)
    raw = raw.assign(Ticker=raw.Ticker.map(lambda t: final.get(t, t)))
    splits_path = data_dir(cfg) / "splits.csv"
    ys = pd.read_csv(splits_path, dtype={"Ticker": str, "ExDate": str}) if splits_path.exists() else pd.DataFrame(columns=["Ticker", "ExDate"])
    ysplit = {t: list(g.ExDate) for t, g in ys.groupby("Ticker")}
    within, total, off, vol_off, mine, theirs, matched, cut_rows = 0, 0, [], [], 0, 0, 0, []
    extra, missing, cash = [], [], 0
    for t, y in yahoo.items():
        g = raw[raw.Ticker == t]
        if len(g) < 30:
            continue
        derived, rep = adjust.adjust_security(g, t, tol)
        ev = rep["events"]
        cash += int((ev.kind == "move").sum())
        if rep["cutAt"]:
            cut_rows.append((t, rep["cutAt"][-1]))
        d_splits = list(ev[ev.kind.isin(["split", "reverse"])].Date)
        mine += len(d_splits)
        theirs += len(ysplit.get(t, []))
        for d in d_splits:
            if bhav.near(d, ysplit.get(t, [])):
                matched += 1
            else:
                extra.append((t, d))
        missing += [(t, d) for d in ysplit.get(t, []) if not bhav.near(d, d_splits) and d >= derived.Date.min()]
        m = derived.merge(y, on="Date", suffixes=("_d", "_y"))
        m = m[m.Date >= clean_from]
        if len(m) < 20:
            continue
        r = m.Close_d / m.Close_y
        within += int((abs(r - 1) < 0.01).sum())
        total += len(r)
        if abs(float(r.median()) - 1) > 0.02:
            off.append((t, round(float(r.median()), 3)))
        vr = (m.Volume_d / m.Volume_y.replace(0, np.nan)).median()
        if np.isfinite(vr) and abs(vr - 1) > 0.10:
            vol_off.append((t, round(float(vr), 2)))
    return "\n".join([
        f"close within 1% of Yahoo on {100 * within / max(total, 1):.1f}% of {total} ticker-days from {clean_from}; tickers whose median close ratio is off by >2%: {len(off)} {off[:8]}",
        f"volume median ratio off by >10%: {len(vol_off)} {vol_off[:8]}",
        f"split-like events derived {mine}, Yahoo splits {theirs}, matched {matched}; derived but not in Yahoo {len(extra)} {extra[:6]}; Yahoo but not derived {len(missing)} {missing[:6]}",
        f"large genuine moves kept as they are (volume level unchanged) {cash}; unresolved cuts {len(cut_rows)} {cut_rows[:6]}"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.universe")
    g = parser.add_mutually_exclusive_group(required=True)
    for flag in ("links", "probe-symbolchange", "validate-adjust"):
        g.add_argument(f"--{flag}", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        if args.links:
            print(run_links(cfg))
        elif args.probe_symbolchange:
            print(probe_symbolchange(cfg, bhav.client_for(cfg)))
        else:
            data = prep.load_pit(cfg)
            path = data_dir(cfg) / "symbol_links.csv"
            table = pd.read_csv(path, dtype=str, keep_default_na=False) if path.exists() else pd.DataFrame(columns=links.LINK_COLS)
            print(validate_adjust(cfg, data.series, bhav.load(cfg), table))
        return 0
    except prep.MissingInput as e:
        print(f"universe: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"universe: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
