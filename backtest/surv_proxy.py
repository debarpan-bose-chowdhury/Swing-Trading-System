"""Surveillance proxy: ASM / T2T flags inferred from price behaviour, because no surveillance history is stored.

Two flags per ticker and day, on the stored (split-adjusted) series:
  asm  - repeated circuit-like days: >= minHits days in the last `window` with |close-to-close move| >= movePct and a
         high-low range <= rangePct (the price barely traded away from where it locked). Blocks new buys and warns.
  t2t  - thin trading: the median daily traded value (close x volume) over `window` days is below minValueInr. As in live, a
         held name on the T2T list is exited (risk.json surveillance.exitOn).
GSM is not proxied: it is a regulatory judgement with no price signature.
Every threshold ships unset (null). The proxy runs only once they are filled, and every report labels it a PROVISIONAL proxy.

  python -m backtest.surv_proxy --snapshot     copy the app's retained surveillance_*.json into backtest/data/surveillance/
  python -m backtest.surv_proxy --calibrate    score a grid of thresholds against the snapshots (precision / recall); prints, writes nothing to config
"""

import argparse
import itertools
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import config, pit
from backtest.replay import no_surveillance

KEYS = ("circuit", "thin")


def circuit_hits(df: pd.DataFrame, move: float, rng: float) -> np.ndarray:
    c = df.Close.to_numpy(float)
    ret = np.r_[np.nan, np.abs(c[1:] / c[:-1] - 1)]
    return (ret >= move) & ((df.High.to_numpy(float) - df.Low.to_numpy(float)) / c <= rng)


def asm_flags(data: pit.PitData, move: float, rng: float, window: int, min_hits: int) -> pd.DataFrame:
    """Date-by-ticker booleans."""
    cols = {k: pd.Series(circuit_hits(df, move, rng).astype(float), index=pd.DatetimeIndex(df.Date)).rolling(window).sum() >= min_hits
            for k, df in data.series.items()}
    return pd.DataFrame(cols).fillna(False).astype(bool).sort_index()


def t2t_flags(data: pit.PitData, window: int, min_value: float) -> pd.DataFrame:
    _, value = data.panels()
    return (value.rolling(window, min_periods=window).median() < min_value).fillna(False)


class Proxy:
    def __init__(self, data: pit.PitData, p: dict):
        c, t = p["circuit"], p["thin"]
        self.asm = asm_flags(data, c["movePct"], c["rangePct"], c["window"], c["minHits"])
        self.t2t = t2t_flags(data, t["window"], t["minValueInr"])
        self.universe = set(data.series)

    def flagged(self, asof: str) -> tuple[list[str], list[str]]:
        ts = pd.Timestamp(asof)
        pick = lambda f: sorted(f.columns[f.loc[ts].to_numpy()]) if ts in f.index else []  # noqa: E731
        return pick(self.asm), pick(self.t2t)

    def __call__(self, asof: str) -> dict:
        asm, t2t = self.flagged(asof)
        s = no_surveillance(asof)
        s["data"]["asm"]["ST"] = {t: 1 for t in asm}
        s["data"]["t2t"] = t2t
        s["exits"] = s["data"]
        return s


def surveillance_for(data: pit.PitData, cfg: dict):
    """The callable simulate() takes: the proxy when the thresholds are set and enabled, else the 'not modelled' placeholder."""
    p = cfg["surv"]
    return Proxy(data, p) if p["proxy"] else no_surveillance


def snapshot(cfg: dict) -> int:
    src = Path(cfg["paths"]["appData"]) / "risk" / "surveillance"
    dst = Path(cfg["paths"]["data"]) / "surveillance"
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob("surveillance_????-??-??.json")) if src.exists() else []
    for f in files:
        shutil.copy2(f, dst / f.name)
    return len(files)


def load_snapshots(folder: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(folder.glob("surveillance_????-??-??.json")):
        s = json.loads(f.read_text(encoding="utf-8"))
        if all(v == "ok" for v in s.get("sources", {}).values()):
            out[s["asOf"]] = {"asm": set(s["asm"]["LT"]) | set(s["asm"]["ST"]), "t2t": set(s["t2t"])}
    return out


def _score(pred: dict[str, set], actual: dict[str, set], universe: set) -> dict:
    tp = fp = fn = 0
    for d, a in actual.items():
        a, p = a & universe, pred.get(d, set())
        tp, fp, fn = tp + len(a & p), fp + len(p - a), fn + len(a - p)
    prec, rec = tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0}


def calibrate(data: pit.PitData, snaps: dict[str, dict], grid: dict) -> dict[str, pd.DataFrame]:
    """Score every grid point on the snapshot dates, best F1 first, for each flag."""
    uni, rows = set(data.series), {"asm": [], "t2t": []}
    for move, rng, window, hits in itertools.product(grid["movePct"], grid["rangePct"], grid["circuitWindow"], grid["minHits"]):
        f = asm_flags(data, move, rng, window, hits)
        pred = {d: set(f.columns[f.loc[pd.Timestamp(d)].to_numpy()]) for d in snaps if pd.Timestamp(d) in f.index}
        rows["asm"].append({"movePct": move, "rangePct": rng, "window": window, "minHits": hits, **_score(pred, {d: s["asm"] for d, s in snaps.items()}, uni)})
    for window, value in itertools.product(grid["thinWindow"], grid["minValueInr"]):
        f = t2t_flags(data, window, value)
        pred = {d: set(f.columns[f.loc[pd.Timestamp(d)].to_numpy()]) for d in snaps if pd.Timestamp(d) in f.index}
        rows["t2t"].append({"window": window, "minValueInr": value, **_score(pred, {d: s["t2t"] for d, s in snaps.items()}, uni)})
    return {k: pd.DataFrame(v).sort_values(["f1", "precision"], ascending=False, ignore_index=True) for k, v in rows.items()}


# The search space only; the values it recommends are for you to review and enter in backtest.json.
GRID = {"movePct": [0.02, 0.03, 0.04, 0.05], "rangePct": [0.005, 0.01, 0.02], "circuitWindow": [10, 20, 40], "minHits": [2, 3, 5],
        "thinWindow": [20, 60], "minValueInr": [1e6, 5e6, 1e7, 5e7]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.surv_proxy")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", action="store_true")
    g.add_argument("--calibrate", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        if args.snapshot:
            print(f"surv_proxy: copied {snapshot(cfg)} surveillance files to {cfg['paths']['data']}/surveillance")
            return 0
        from backtest import prep
        snaps = load_snapshots(Path(cfg["paths"]["data"]) / "surveillance")
        if not snaps:
            print("surv_proxy: no usable snapshots (run --snapshot after app.risk.surveillance has produced lists)", file=sys.stderr)
            return 3
        data = prep.load_pit(cfg)
        print(f"surv_proxy: calibrating on {len(snaps)} snapshot days ({min(snaps)} to {max(snaps)}); thresholds stay PROVISIONAL")
        for name, table in calibrate(data, snaps, GRID).items():
            print(f"\n{name}: best 8 of {len(table)}\n{table.head(8).to_string(index=False)}")
        return 0
    except Exception as e:
        print(f"surv_proxy: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
