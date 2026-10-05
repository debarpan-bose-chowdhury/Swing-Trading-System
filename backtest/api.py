"""The public face of the backtest engine: the only module an optimiser (hpo/) imports from backtest/.

    world = build_world()                                   # data, universe, targets machinery; built once
    res = evaluate_config(world, risk, analyst, start, end)  # one judge run for full app configs

evaluate_config takes the complete risk and analyst dicts. The caller applies its own overrides and validates them with the
app's validators (analyst.common.validate, risk.common.validate), so an invalid point is rejected before a run; it does not use the
grid-based params.Schema. It is a thin wrapper over replay.simulate, tax.lots / assess / post_tax_curve and evaluator.perf, the same
calls backtest.run.evaluate and trials.Session.evaluate make (both now call it), so their results are the same by construction.
"""

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd

from app.risk import evaluator
from backtest import config, params, replay, tax, world
from backtest.targets import TargetsCache
from backtest.walkforward import HoldoutRead, Windows  # noqa: F401  (re-exported: the exception holdout_guard raises)
from backtest.world import World

DEFAULT_TARGETS_CACHE = 4


@dataclass
class EvalResult:
    returns: pd.Series  # post-tax daily returns, indexed by ISO date
    nav: pd.DataFrame  # the replay's NAV rows (pre-tax)
    post_tax_nav: pd.Series
    fills: pd.DataFrame
    taxes: dict  # tax.assess output, by financial year
    metrics: dict  # evaluator.perf of the post-tax returns
    hashes: dict  # config, data, code
    result: replay.Result = field(repr=False, default=None)
    pieces: pd.DataFrame = field(repr=False, default=None)  # tax.lots output


@lru_cache(maxsize=1)
def _code_sha() -> str:
    return world.code_sha()


def build_world(cfg_overrides: dict | None = None, *, targets_cache_size: int = DEFAULT_TARGETS_CACHE) -> World:
    """World from backtest.json (plus a deep-merged partial override, validated by config.validate) and the stored data."""
    cfg = config.load()
    if cfg_overrides:
        cfg = world.deep_merge(cfg, cfg_overrides)
        config.validate(cfg)
    w = World.build(cfg)
    targets_cache(w, targets_cache_size)
    return w


def targets_cache(w: World, size: int = DEFAULT_TARGETS_CACHE) -> TargetsCache:
    if "targets" not in w.cache:
        w.cache["targets"] = TargetsCache(w.data, size)
        w.cache["targets"].put(w.analyst, w.targets)  # the world's own targets are already built
    return w.cache["targets"]


def data_hash(w: World) -> str:
    if "dataHash" not in w.cache:
        w.cache["dataHash"] = w.data.data_hash()
    return w.cache["dataHash"]


def evaluate_config(w: World, risk: dict, analyst: dict, start: str, end: str | None, *, haircut: float = 0.0, capital: float | None = None,
                    progress=None) -> EvalResult:
    """One judge run of the app configs `risk` and `analyst` over [start, end] on the world's data, taxed with the world's schedule.

    haircut: write-off share of a position whose ticker stopped trading (0 = at the last close, 1 = total loss).
    capital: starting rupees, default backtest.json capital.inr.
    """
    cfg = w.cfg
    result = replay.simulate(w.data, targets_cache(w).get(analyst), risk, start, end, cfg["capital"]["inr"] if capital is None else capital,
                             w.surveillance, carry_over_days=config.carry_over_days(cfg, risk), dividends=w.dividends, vanish_haircut=haircut,
                             progress=progress, restart_after=config.restart_after(cfg))
    if result.nav.empty:
        raise ValueError(f"no simulated days between {start} and {end}")
    pieces = tax.lots(result.fills)
    taxes = tax.assess(pieces, cfg["tax"]["schedule"])
    post = tax.post_tax_curve(result.nav, taxes)
    r = post.pct_change().dropna()
    metrics = evaluator.perf(r, risk["evaluator"]["riskFreeRatePct"], days=evaluator.days_of(risk))
    hashes = {"config": world.config_hash(cfg, risk, analyst), "data": data_hash(w), "code": _code_sha()}
    return EvalResult(r, result.nav, post, result.fills, taxes, metrics, hashes, result, pieces)


def windows(w: World, schema: params.Schema | None = None) -> Windows:
    """Holdout, tuning end, folds and purge for the world's index, from backtest.json and the parameter bounds."""
    schema = schema or params.Schema.load(w.cfg["paths"]["params"], w.risk, w.analyst)
    dates = list(w.data.index.Date)
    return Windows(dates, schema.common_start(dates), w.cfg["walkforward"], w.cfg["window"]["holdoutYears"], schema.required_purge())


def holdout_guard(win: Windows, marker: Path, params_key: str) -> tuple[str, str]:
    """The holdout window, once: the marker file refuses a second, different parameter set (raises HoldoutRead)."""
    return win.holdout(marker, params_key)


def point_key(point: dict) -> str:
    return params.key_of(point)


def trial_id(params_key: str, start: str, end: str, kind: str) -> str:
    return hashlib.sha256(f"{params_key}|{start}|{end}|{kind}".encode()).hexdigest()[:12]
