"""The tunable parameters: bounds, steps, constraints, and how a point (key -> value) becomes in-memory app configs.

Bounds live in backtest/config/params.json. A point is applied over the live risk and analyst configs by writing each parameter's
paths (a "*" segment fans out over every key at that level; a numeric segment indexes a list), then running the app's own config
validators, so an invalid combination is rejected before a run by the same rules the live system enforces.
"""

import copy
import json
import random
from dataclasses import dataclass
from pathlib import Path

from app.analyst import common as analyst_common
from app.analyst.regime import UNKNOWN_EXTRA
from app.risk import common as risk_common
from app.market.common import safe_path

LONGEST_WINDOW_KEYS = ("selector.lookback.", "selector.trendMa", "regime.smaSlow", "regime.momentumDays")  # look-back windows, in trading days
MIN_PURGE = 168  # trading days; backtest.json walkforward.purgeDays cannot go below it


class InvalidPoint(ValueError):
    """A parameter point outside its bounds or grid, or one the app's config rules reject."""


@dataclass(frozen=True)
class Param:
    key: str
    group: str
    target: str  # "risk" or "analyst"
    paths: tuple
    low: float
    high: float
    step: float
    kind: str

    def values(self) -> list:
        n = round((self.high - self.low) / self.step)
        return [self._cast(self.low + i * self.step) for i in range(n + 1)]

    def _cast(self, v):
        return int(round(v)) if self.kind == "int" else round(float(v), 10)

    def snap(self, v):
        """The grid value nearest to v inside the bounds."""
        i = min(max(round((v - self.low) / self.step), 0), round((self.high - self.low) / self.step))
        return self._cast(self.low + i * self.step)

    def on_grid(self, v) -> bool:
        return self.low - 1e-9 <= v <= self.high + 1e-9 and abs((v - self.low) / self.step - round((v - self.low) / self.step)) < 1e-6


def _segments(cfg, segs: list[str]):
    """Yield the concrete segment lists a path with "*" expands to."""
    if not segs:
        yield []
        return
    head, rest = segs[0], segs[1:]
    keys = list(cfg) if head == "*" else [head]
    for k in keys:
        child = cfg[int(k)] if isinstance(cfg, list) else cfg[k]
        for tail in _segments(child, rest):
            yield [k, *tail]


def get_path(cfg, segs: list[str]):
    for s in segs:
        cfg = cfg[int(s)] if isinstance(cfg, list) else cfg[s]
    return cfg


def set_path(cfg, segs: list[str], value) -> None:
    parent = get_path(cfg, segs[:-1])
    if isinstance(parent, list):
        parent[int(segs[-1])] = value
    else:
        parent[segs[-1]] = value


class Schema:
    def __init__(self, doc: dict, risk: dict, analyst: dict):
        self.confirmed, self.constraints = bool(doc["confirmed"]), doc.get("constraints", [])
        self.params = [Param(p["key"], p["group"], p["target"], tuple(p["paths"]), p["low"], p["high"], p["step"], p["kind"]) for p in doc["params"]]
        self.by_key = {p.key: p for p in self.params}
        if len(self.by_key) != len(self.params):
            raise ValueError("params.json: duplicate keys")
        self.base = {"risk": risk, "analyst": analyst}
        for p in self.params:
            if p.target not in self.base or p.low >= p.high or p.step <= 0:
                raise ValueError(f"params.json: {p.key} needs target risk/analyst, low < high, step > 0")
            for path in p.paths:
                list(_segments(self.base[p.target], path.split(".")))  # a path that does not exist in the live config fails here

    @classmethod
    def load(cls, path: str | Path, risk: dict, analyst: dict) -> "Schema":
        return cls(json.loads(safe_path(path).read_text(encoding="utf-8")), risk, analyst)

    def live_value(self, p: Param):
        """The live config's value (the first path's; the paths of one parameter move together)."""
        first = next(_segments(self.base[p.target], p.paths[0].split(".")))
        return get_path(self.base[p.target], first)

    def defaults(self) -> dict:
        """The live values snapped into the bounds. clipped() lists the ones the bounds moved."""
        return {p.key: p.snap(self.live_value(p)) for p in self.params}

    def clipped(self) -> dict:
        return {p.key: (self.live_value(p), p.snap(self.live_value(p))) for p in self.params if abs(self.live_value(p) - p.snap(self.live_value(p))) > 1e-9}

    def check(self, point: dict, strict: bool = True) -> None:
        unknown = set(point) - set(self.by_key)
        if unknown:
            raise InvalidPoint(f"unknown parameters: {sorted(unknown)}")
        for k, v in point.items():
            p = self.by_key[k]
            if strict and not p.on_grid(v):
                raise InvalidPoint(f"{k}={v} is off the grid {p.low}..{p.high} step {p.step}")
        merged = {**self.defaults(), **point}
        for c in self.constraints:
            a, b = c["le"]
            if merged[a] > merged[b]:
                raise InvalidPoint(f"constraint {a} <= {b} violated ({merged[a]} > {merged[b]})")

    def apply(self, point: dict, strict: bool = True) -> tuple[dict, dict]:
        """(risk, analyst) configs for a point (omitted parameters keep their live value), validated by the app's own rules."""
        self.check(point, strict)
        cfgs = {t: copy.deepcopy(c) for t, c in self.base.items()}
        for k, v in point.items():
            p = self.by_key[k]
            for path in p.paths:
                for segs in _segments(cfgs[p.target], path.split(".")):
                    set_path(cfgs[p.target], segs, v)
        r = cfgs["analyst"]["regime"]
        r["minRows"] = max(r["minRows"], max(r.get("smaSlow", 200), r.get("momentumDays", 63)) + 10)  # data sufficiency follows the windows
        try:
            analyst_common.validate(cfgs["analyst"])
            risk_common.validate(cfgs["risk"], "run")
        except (ValueError, KeyError) as e:
            raise InvalidPoint(str(e)) from e
        return cfgs["risk"], cfgs["analyst"]

    def sample(self, rng: random.Random, tries: int = 200) -> dict:
        """A random valid point (rejection sampling against the app's validators)."""
        for _ in range(tries):
            point = {p.key: rng.choice(p.values()) for p in self.params}
            try:
                self.apply(point)
                return point
            except InvalidPoint:
                continue
        raise InvalidPoint("no valid point found; the bounds are probably too tight")

    def neighbours(self, point: dict) -> list[dict]:
        """Valid points one grid step away in a single parameter (both directions)."""
        full, out = {**self.defaults(), **point}, []
        for p in self.params:
            vals = p.values()
            i = vals.index(full[p.key])
            for j in (i - 1, i + 1):
                if 0 <= j < len(vals):
                    q = {**full, p.key: vals[j]}
                    try:
                        self.apply(q)
                        out.append(q)
                    except InvalidPoint:
                        pass
        return out

    def required_purge(self) -> int:
        """Longest look-back any allowed point can use, in trading days (never below MIN_PURGE)."""
        longest = [p.high for p in self.params if p.key.startswith(LONGEST_WINDOW_KEYS)]
        return max([MIN_PURGE, *map(int, longest)])

    def warmup_rows(self) -> int:
        """Index rows before the slowest allowed regime is known (UNKNOWN_EXTRA past the longest regime window)."""
        longest = max(self.by_key["regime.smaSlow"].high, self.by_key["regime.momentumDays"].high)
        return int(longest) + UNKNOWN_EXTRA

    def common_start(self, index_dates: list[str]) -> str:
        """First day every allowed point has a known regime, plus the longest allowed persistence (5 sessions a week)."""
        n = self.warmup_rows() + 5 * int(self.by_key["regime.persistenceWeeks"].high)
        if n >= len(index_dates):
            raise ValueError(f"the index has {len(index_dates)} rows; the slowest regime needs {n}")
        return index_dates[n]


def key_of(point: dict) -> str:
    return json.dumps(point, sort_keys=True, separators=(",", ":"))
