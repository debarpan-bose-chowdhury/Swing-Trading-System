"""Point-in-time universe tooling (Phase 7b). Run from the repo root.

  python -m backtest.universe --links                 build symbol_links.csv (ISIN chain + manual + NSE) and symbol_review.csv
  python -m backtest.universe --probe-symbolchange    download NSE's symbol-change file once and print its header (network)
  python -m backtest.universe --build-pit              rank, price and label the point-in-time universe -> backtest/data/pit/
  python -m backtest.universe --tune-adjust            score a grid of detector settings against Yahoo's splits
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


def load_truth(cfg: dict) -> dict[str, pd.DataFrame]:
    """Yahoo's split table per ticker (ExDate, Ratio), the ground truth for the names Yahoo covers."""
    path = data_dir(cfg) / "splits.csv"
    if not path.exists():
        return {}
    ys = pd.read_csv(path, dtype={"Ticker": str, "ExDate": str})
    return {t: g.reset_index(drop=True) for t, g in ys.groupby("Ticker")}


def prepared(raw: pd.DataFrame, table: pd.DataFrame, names) -> dict[str, pd.DataFrame]:
    """raw bhavcopy rows per final symbol (renames merged), for the given names."""
    final = links.resolve(table)
    r = raw.assign(Ticker=raw.Ticker.map(lambda t: final.get(t, t)))
    r = r[r.Ticker.isin(set(names))]
    return {t: g.sort_values(["Date"]).drop_duplicates("Date", keep="last") for t, g in r.groupby("Ticker")}


def score_events(tol: dict, rows: dict[str, pd.DataFrame], truth: dict[str, pd.DataFrame]) -> dict:
    """Event-level agreement with Yahoo's splits: matched, missed, extra (derived but not in Yahoo), cuts, and details of the misses."""
    matched = extra = cuts = n_true = 0
    missed, extras, cut_list = [], [], []
    for t, g in rows.items():
        if len(g) < 30:
            continue
        derived, rep = adjust.adjust_security(g, t, tol)
        mine = list(rep["events"][rep["events"].kind.isin(["split", "reverse"])].Date)
        yahoo = [d for d in (truth[t].ExDate if t in truth else []) if d >= derived.Date.min()]
        n_true += len(yahoo)
        for d in mine:
            if bhav.near(d, yahoo):
                matched += 1
            else:
                extras.append((t, d))
        for d in yahoo:
            if not bhav.near(d, mine):
                missed.append((t, d))
        if rep["cutAt"]:
            cuts += 1
            cut_list.append((t, rep["cutAt"][-1]))
    return {"matched": matched, "yahoo": n_true, "extra": len(extras), "cuts": cuts, "missed": missed, "extras": extras, "cutList": cut_list}


def explain_miss(g: pd.DataFrame, day: str, tol: dict, yahoo_ratio) -> str:
    """What the tape looked like around a split Yahoo knows and the detector missed: price move, usual factor, volume shift, decision."""
    g = g.reset_index(drop=True)
    i = int(np.searchsorted(g.Date.to_numpy(), day))
    if not 0 < i < len(g):
        return f"{day}: outside the bhavcopy range"
    close, vol = g.Close.to_numpy(float), g.Volume.to_numpy(float)
    r = close[i] / close[i - 1]
    k = 1 / r if r < 1 else r
    w = tol["volumeWindow"]
    pre, post = np.median(vol[max(0, i - w):i]), np.median(vol[i:i + w])
    ev = adjust.events(g, tol)
    kind = ev[ev.Date == g.Date.iloc[i]].kind.tolist()
    return (f"{g.Date.iloc[i]} Yahoo ratio {yahoo_ratio}: close x{r:.3f} (factor {k:.2f}, usual {adjust.nearest_nice(k, tol['niceTolerance'])}), "
            f"volume {pre:,.0f} -> {post:,.0f} (x{post / pre if pre else float('nan'):.2f}), decided {kind[0] if kind else 'no move over the threshold'}")


def validate_adjust(cfg: dict, yahoo: dict[str, pd.DataFrame], raw: pd.DataFrame, table: pd.DataFrame, clean_from: str = "2012-01-01") -> str:
    """Derive adjusted series from the raw bhavcopy for names Yahoo also has, and compare. Dates before clean_from are skipped (Yahoo's 2007-09 closes are noisy)."""
    tol = cfg["universe"]["adjust"]
    truth, rows = load_truth(cfg), prepared(raw, table, yahoo)
    sc = score_events(tol, rows, truth)
    within, total, off, vol_off, moves = 0, 0, [], [], 0
    for t, y in yahoo.items():
        g = rows.get(t)
        if g is None or len(g) < 30:
            continue
        derived, rep = adjust.adjust_security(g, t, tol)
        moves += int((rep["events"].kind == "move").sum())
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
    recall = sc["matched"] / max(sc["yahoo"], 1)
    lines = [
        f"close within 1% of Yahoo on {100 * within / max(total, 1):.1f}% of {total} ticker-days from {clean_from}; tickers whose median close ratio is off by >2%: {len(off)} {off[:8]}",
        f"volume median ratio off by >10%: {len(vol_off)} {vol_off[:8]}",
        f"splits: Yahoo {sc['yahoo']} in range, matched {sc['matched']} (recall {100 * recall:.0f}%), derived but not in Yahoo {sc['extra']} {sc['extras'][:6]}",
        f"large genuine moves kept as they are (volume level unchanged) {moves}; names cut at an unresolved break {sc['cuts']} {sc['cutList'][:6]}",
        "missed splits, what the tape showed (first 12):"]
    ratio = {t: dict(zip(g.ExDate, g.Ratio)) for t, g in truth.items()}
    for t, d in sorted(sc["missed"], key=lambda x: x[1])[:12]:
        lines.append(f"  {t} " + explain_miss(rows[t], d, tol, ratio.get(t, {}).get(d)))
    return "\n".join(lines)


GRID = {"volumeTolerance": [0.4, 0.8], "volumeWindow": [10, 20], "niceTolerance": [0.05, 0.08], "tightTolerance": [0.0, 0.03, 0.05]}


def tune_adjust(cfg: dict, yahoo: dict[str, pd.DataFrame], raw: pd.DataFrame, table: pd.DataFrame) -> str:
    """Score a grid of detector settings against Yahoo's splits: recall, false events, names cut. Writes nothing to config."""
    import itertools
    truth, rows = load_truth(cfg), prepared(raw, table, yahoo)
    out = []
    for vt, vw, nt, tt in itertools.product(GRID["volumeTolerance"], GRID["volumeWindow"], GRID["niceTolerance"], GRID["tightTolerance"]):
        tol = {**cfg["universe"]["adjust"], "volumeTolerance": vt, "volumeWindow": vw, "niceTolerance": nt, "tightTolerance": tt}
        sc = score_events(tol, rows, truth)
        out.append((vt, vw, nt, tt, sc["matched"], sc["yahoo"], sc["extra"], sc["cuts"]))
    t = pd.DataFrame(out, columns=["volumeTol", "volWindow", "niceTol", "tightTol", "matched", "yahooSplits", "falseEvents", "namesCut"])
    t["recall%"] = (100 * t.matched / t.yahooSplits.clip(lower=1)).round(0)
    return "recall = share of Yahoo's splits found; falseEvents = found but not in Yahoo (a lower bound on false positives); namesCut = names whose history is cut; tightTol 0 turns the price-only rule off\n" + \
        t.sort_values(["recall%", "falseEvents"], ascending=[False, True]).to_string(index=False)


def run_build_pit(cfg: dict) -> str:
    from backtest import pituniverse
    data = prep.load_pit({**cfg, "universe": {**cfg["universe"], "mode": "today"}})  # Yahoo's series; the point-in-time layer is what gets built
    r = pituniverse.build(cfg, data.series, list(data.index.Date))
    h = r["holesPerDate"]
    return "\n".join([
        f"scope: {r['scope']} names ever in the top {cfg['universe']['scopeTop']} at a month end: {r['fromYahoo']} priced from Yahoo, {r['derived']} derived from the bhavcopy, "
        f"{r['scopeWithoutUsableSeries']} without a usable series",
        f"unresolved corporate-action breaks (series cut): {r['cuts']} e.g. {r['cutExamples'][:6]}",
        f"membership: {r['membershipRows']} rows over {r['rebalanceDates']} rebalance dates",
        f"holes (top-150 slots held by names with no usable series, skipped): mean {h['mean']} per date, max {h['max']}; by year {h['byYear']}"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.universe")
    g = parser.add_mutually_exclusive_group(required=True)
    for flag in ("links", "probe-symbolchange", "validate-adjust", "tune-adjust", "build-pit"):
        g.add_argument(f"--{flag}", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        if args.links:
            print(run_links(cfg))
        elif args.build_pit:
            print(run_build_pit(cfg))
        elif args.probe_symbolchange:
            print(probe_symbolchange(cfg, bhav.client_for(cfg)))
        else:
            data = prep.load_pit(cfg)
            path = data_dir(cfg) / "symbol_links.csv"
            table = pd.read_csv(path, dtype=str, keep_default_na=False) if path.exists() else pd.DataFrame(columns=links.LINK_COLS)
            raw = bhav.load(cfg, set(data.series) | set(table.Old))
            print(tune_adjust(cfg, data.series, raw, table) if args.tune_adjust else validate_adjust(cfg, data.series, raw, table))
        return 0
    except prep.MissingInput as e:
        print(f"universe: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"universe: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
