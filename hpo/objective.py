"""One continuous simulation per configuration, sliced into folds; objectives, constraints, aborts and failure mapping.

Parameters are fixed inside a trial, so a walk-forward fold is a measurement window of one run over the whole pre-holdout span:
`api.evaluate_config` runs once and the per-fold figures come from the stored post-tax daily returns. Open positions and the ladder
carry across fold boundaries, as in live trading.

Objectives (both from that one run):
  f1 = CVaR over the worst `worstShare` of the fold post-tax CAGRs   (maximise)
  f2 = depth of the full-span post-tax max drawdown, as a positive fraction   (minimise)
Constraints are numbers with value <= 0 feasible (the Optuna convention): dd_cap, min_fills, fills_per_fold_year, min_exposure, aborted,
valid. A trial that cannot be scored (rejected point, aborted run) carries the worst possible objectives as placeholders, never read as
performance: the constraints make it infeasible and every report greys it out.
"""

import json
import math
import time

import numpy as np
import pandas as pd

from backtest import api
from hpo import space as space_mod

WORST = (-1.0, 1.0)  # placeholder objectives of a trial that has none (post-tax CAGR -100%, drawdown depth 100%)
CONSTRAINT_KEYS = ("valid", "aborted", "dd_cap", "min_fills", "fills_per_fold_year", "min_exposure")


class AbortRun(Exception):
    """Infeasibility, never performance: the run is stopped because it cannot become a feasible trial."""

    def __init__(self, reason: str, asof: str | None = None, depth: float | None = None):
        super().__init__(reason)
        self.reason, self.asof, self.depth = reason, asof, depth


def cvar_worst(values: list[float], share: float) -> float:
    """Mean of the worst ceil(share * n) values (at least one)."""
    v = sorted(values)
    return float(np.mean(v[:max(1, math.ceil(share * len(v)))]))


def make_monitor(limits: dict, days_per_year: int):
    """The replay monitor: stop when the running drawdown is worse than abortDrawdown, or nothing has traded after abortNoFillYears."""
    floor, years = limits["abortDrawdown"], limits["abortNoFillYears"]

    def monitor(rows: list, n_fills: int) -> None:
        nav = np.array([float(r["nav"]) for r in rows], float)
        if len(nav):
            depth = float((nav / np.maximum.accumulate(nav)).min() - 1.0)
            if depth < floor:
                raise AbortRun("drawdown", str(rows[-1]["date"]), -depth)
            if n_fills == 0 and len(nav) > years * days_per_year:
                raise AbortRun("no fills", str(rows[-1]["date"]), -depth)
    return monitor


def _slice(r: pd.Series, first: str, last: str) -> pd.Series:
    return r[(r.index >= first) & (r.index <= last)]


def fold_stats(returns: pd.Series, folds: list[tuple[str, str]], fills: pd.DataFrame, rf: float, days: int) -> list[dict]:
    """Per fold: post-tax CAGR and max drawdown over the test window of the continuous run, and the fills per year inside it."""
    out = []
    for first, last in folds:
        r = _slice(returns, first, last)
        perf = api.perf(r, rf, days=days) if len(r) >= 2 else {}
        n = int(((fills.trade_date >= first) & (fills.trade_date <= last)).sum()) if len(fills) and "trade_date" in fills else 0
        years = len(r) / days
        out.append({"first": first, "last": last, "days": len(r), "cagr": perf.get("cagr") if perf.get("cagr") is not None else -1.0,
                    "maxDrawdown": perf.get("maxDrawdown", 0.0), "fills": n, "fillsPerYear": n / years if years else 0.0})
    return out


def avg_exposure(nav: pd.DataFrame) -> float:
    """Average gross exposure: positions value over NAV, across the run."""
    if nav.empty:
        return 0.0
    pv, total = pd.to_numeric(nav.positions_value, errors="coerce"), pd.to_numeric(nav.nav, errors="coerce")
    ratio = (pv / total).replace([np.inf, -np.inf], np.nan).dropna()
    return float(ratio.mean()) if len(ratio) else 0.0


def score(returns: pd.Series, nav: pd.DataFrame, fills: pd.DataFrame, metrics: dict, folds: list[tuple[str, str]], cfg: dict, rf: float, days: int) -> dict:
    """Objectives, constraints and the report-only figures of one finished run."""
    c, share = cfg["constraints"], cfg["objectives"]["cagr"]["worstShare"]
    stats = fold_stats(returns, folds, fills, rf, days)
    f1 = cvar_worst([s["cagr"] for s in stats], share) if stats else WORST[0]
    depth = -float(metrics.get("maxDrawdown", 0.0))
    expo, n_fills = avg_exposure(nav), int(len(fills))
    worst_year = min((s["fillsPerYear"] for s in stats), default=0.0)
    cons = {"valid": 0.0, "aborted": 0.0, "dd_cap": depth + c["maxDrawdown"], "min_fills": c["minFills"] - n_fills,
            "fills_per_fold_year": c["minFillsPerFoldYear"] - worst_year, "min_exposure": c["minAvgExposure"] - expo}
    return {"values": [f1, depth], "constraints": cons,
            "metrics": {"cagr": metrics.get("cagr"), "maxDrawdown": metrics.get("maxDrawdown"), "ulcerIndex": metrics.get("ulcerIndex"), "calmar": metrics.get("calmar"),
                        "sortino": metrics.get("sortino"), "sharpe": metrics.get("sharpe"), "drawdownDurationDays": metrics.get("drawdownDurationDays"),
                        "avgExposure": expo, "fills": n_fills, "foldCagr": [s["cagr"] for s in stats], "foldDrawdown": [s["maxDrawdown"] for s in stats],
                        "foldFillsPerYear": [s["fillsPerYear"] for s in stats]}}


FEE_KEYS = ("pct", "flatInr", "minInr")  # inside costs.brokerage
CHARGE_KEYS = ("sttPct", "nseTxnPct", "ipftPct", "sebiPct", "stampBuyPct", "dpSellInr")  # taxes' base amounts scale; GST rates do not


def apply_stress(risk: dict, stress: dict | None) -> dict:
    """A copy of the risk config with the simulator's cost assumptions stressed: slippage x slippageMult, every fee and charge x chargesMult."""
    if not stress:
        return risk
    out = {**risk, "costs": json.loads(json.dumps(risk["costs"]))}
    c = out["costs"]
    for b, v in c["slippageBpsPerSide"].items():
        c["slippageBpsPerSide"][b] = v * stress.get("slippageMult", 1.0)
    mult = stress.get("chargesMult", 1.0)
    for k in FEE_KEYS:
        c["brokerage"][k] = c["brokerage"][k] * mult
    for k in CHARGE_KEYS:
        c[k] = c[k] * mult
    return out


def window_stats(returns: pd.Series, spans: list, rf: float, days: int) -> dict | None:
    """Compounded return and max drawdown over the days of a named stress window (its spans joined), None when under two days overlap."""
    r = pd.concat([_slice(returns, a, b) for a, b in spans])
    if len(r) < 2:
        return None
    p = api.perf(r, rf, days=days)
    return {"days": len(r), "return": float((1 + r).prod() - 1), "maxDrawdown": p.get("maxDrawdown", 0.0), "spans": [list(x) for x in spans]}


def regime_stats(returns: pd.Series, regime: pd.Series, rf: float, days: int) -> dict:
    """Per regime (BULL, TREND, WEAK, BEAR, Unknown): share of days, annualised post-tax return and max drawdown of that regime's days joined."""
    out = {}
    reg = regime.reindex(returns.index)
    for name, r in returns.groupby(reg):
        p = api.perf(r, rf, days=days) if len(r) >= 2 else {}
        out[str(name)] = {"share": len(r) / len(returns), "cagr": p.get("cagr"), "maxDrawdown": p.get("maxDrawdown")}
    return out


def fills_profile(fills: pd.DataFrame, nav: pd.Series) -> dict:
    """Trade profile: fills per calendar year, turnover (traded value over mean NAV, per year), charges as a share of mean NAV per year, open names by month."""
    if not len(fills):
        return {"fillsPerYear": {}, "turnover": {}, "costDrag": {}, "names": {}}
    f = fills.assign(year=fills.trade_date.str[:4], value=fills.qty * fills.price)
    mean_nav = nav.groupby(nav.index.str[:4]).mean()
    held, names = {}, {}
    for d, side, t, q in zip(f.trade_date, f.side, f.ticker, f.qty):
        held[t] = held.get(t, 0) + (q if side == "BUY" else -q)
        names[d[:7]] = sum(v > 0 for v in held.values())
    per = lambda col: {y: float(v / mean_nav.get(y, float("nan"))) for y, v in f.groupby("year")[col].sum().items() if y in mean_nav.index}  # noqa: E731
    return {"fillsPerYear": {y: int(n) for y, n in f.groupby("year").size().items()}, "turnover": per("value"), "costDrag": per("charges"), "names": names}


def feasible(constraints: dict) -> bool:
    return all(v <= 0 for v in constraints.values())


def finite(outcome: dict) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in [*outcome["values"], *outcome["constraints"].values()])


def placeholder(status: str, **attrs) -> dict:
    """An outcome without a score: worst-case objectives, every constraint that applies marked violated."""
    cons = {k: 0.0 for k in CONSTRAINT_KEYS}
    cons["valid" if status == "invalid" else "aborted"] = 1.0
    return {"status": status, "values": list(WORST), "constraints": cons, "metrics": {}, "returns": None, "regimeShare": {}, "attrs": attrs}


class BacktestRunner:
    """Worker-side evaluator: the world, the space and the windows are built once; run() maps one point to one outcome dict."""

    def __init__(self, settings: dict, world=None, windows=None, folds: list[tuple[str, str]] | None = None, schema_path: str | None = None):
        """world, windows, folds and schema_path are for tests and replays; a study builds everything from the settings and the stored data."""
        self.cfg = settings
        self.w = world or api.build_world({"universe": {"mode": settings["universe"]["selection"]}}, targets_cache_size=settings["compute"]["targetsCache"])
        self.space = space_mod.load(settings, self.w.risk, self.w.analyst, schema_path)
        dates = list(self.w.data.index.Date)
        self.windows = windows or api.windows(self.w, start=dates[self.space.warmup_rows()], required_purge=self.space.longest_lookback())
        self.folds = folds or [f.test for f in self.windows.rolling()]
        if not self.folds:
            raise ValueError("no walk-forward folds fit before the holdout: check backtest.json window and walkforward")
        self.span = (self.windows.start, self.windows.tuning_end)
        self.haircut = settings["universe"]["writeOff"]
        self.days = self.w.risk["evaluator"].get("tradingDaysPerYear", 252)
        self.rf = self.w.risk["evaluator"]["riskFreeRatePct"]
        self._alt: dict = {}  # stress worlds, built on first use

    def identity(self) -> dict:
        """What a cached result depends on besides the point: data, code, base configs, span, folds, write-off."""
        return {"dataHash": api.data_hash(self.w), "codeSha": api._code_sha(), "baseConfig": api.config_hash(self.w.cfg, self.w.risk, self.w.analyst),
                "span": list(self.span), "folds": [list(f) for f in self.folds], "writeOff": self.haircut, "schemaVersion": self.space.version}

    def world_for(self, stress: dict | None):
        """The world a stressed run uses: the base one, a copy without a share of the names, or the other universe mode (built once, on demand)."""
        stress = stress or {}
        w = self.w
        mode = stress.get("universe")
        if mode and mode != self.cfg["universe"]["selection"]:
            if mode not in self._alt:
                self._alt[mode] = api.build_world({"universe": {"mode": mode}}, targets_cache_size=self.cfg["compute"]["targetsCache"])
            w = self._alt[mode]
        if stress.get("dropNames"):
            key = (mode, stress["dropNames"], stress.get("seed", 1))
            if key not in self._alt:
                self._alt[key] = api.drop_names(w, stress["dropNames"], stress.get("seed", 1))
            w = self._alt[key]
        return w

    def detail_of(self, ev, w) -> dict:
        """Everything a candidate report draws, from one run: curves, regime and stress-window figures, the trade profile, vanished names."""
        nav = ev.nav.set_index("date")
        regime = nav.active_regime
        windows = {n: window_stats(ev.returns, spans, self.rf, self.days) for n, spans in w.cfg["stress"].items()}
        bench = pd.to_numeric(nav.bench_close, errors="coerce")
        v = ev.result.vanished
        return {"nav": ev.post_tax_nav, "bench": bench / bench.dropna().iloc[0] if bench.notna().any() else bench, "exposure": (pd.to_numeric(nav.positions_value) / pd.to_numeric(nav.nav)),
                "rung": pd.to_numeric(nav.rung), "regime": regime, "regimes": regime_stats(ev.returns, regime, self.rf, self.days),
                "stressWindows": {k: x for k, x in windows.items() if x}, "profile": fills_profile(ev.fills, ev.post_tax_nav),
                "vanished": {"exits": len(v), "writtenOffInr": float(sum(x.get("writtenOffInr", 0.0) for x in v))},
                "surveillanceModelled": bool(w.cfg["surv"]["proxy"]), "realism": dict(w.cfg["fill"]["realism"])}

    def run(self, job: dict) -> dict:
        """job: {"values": point} plus optional "stress": {slippageMult, chargesMult, writeOff, dropNames, universe, seed} and "detail": True (curves and breakdowns), "noAbort": True (a baseline such as the live default is run to the end even when it never trades)."""
        t0 = time.time()
        stress = job.get("stress")
        try:
            risk, analyst, full = self.space.decode(job["values"])
        except space_mod.InvalidPoint as e:
            out = placeholder("invalid", error=str(e))
        else:
            self.windows.check_tuning(*self.span)
            try:
                w = self.world_for(stress)
                haircut = (stress or {}).get("writeOff", self.haircut)
                ev = api.evaluate_config(w, apply_stress(risk, stress), analyst, *self.span, haircut=haircut, monitor=None if job.get("noAbort") else make_monitor(self.cfg["constraints"], self.days))
            except AbortRun as a:
                out = placeholder("aborted", abortReason=a.reason, abortedAt=a.asof, depthAtAbort=a.depth)
            except Exception as e:  # noqa: BLE001  a crashed trial is recorded as FAIL, never retried silently
                out = {**placeholder("fail"), "error": f"{type(e).__name__}: {e}"}
            else:
                out = {"status": "ok", **score(ev.returns, ev.nav, ev.fills, ev.metrics, self.folds, self.cfg, self.rf, self.days), "returns": ev.returns, "attrs": {}}
                share = ev.nav.active_regime.value_counts(normalize=True)
                out["regimeShare"] = {k: float(share.get(k, 0.0)) for k in space_mod.REGIMES}
                if job.get("detail"):
                    out["detail"] = self.detail_of(ev, w)
                if not finite(out):
                    out = {**placeholder("fail"), "error": "non-finite objective or constraint"}
        out["seconds"] = round(time.time() - t0, 2)
        return out
