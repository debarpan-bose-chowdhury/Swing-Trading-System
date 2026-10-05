"""The typed, hierarchical parameter space: the single source of truth for what hpo may move.

Two steps. `build_schema` turns the parameter register (doc/parameter_register.csv, S1) plus the proposed bounds in
hpo/config/space_extra.json into hpo/schema/parameters.schema.json (names, kinds, bounds, class, group, stage and a decode role per
dimension; no defaults, so a changed live config never makes the file stale). `ParameterSpace` loads that file over the live risk and
analyst configs: the defaults are the live values (trial 0 of every study), a bound that excludes the live value is widened to it, and
`decode` turns a point into full app configs.

Constraints are met by construction, never by penalty: smaFast + smaGap, clamp lo + width, minNewOrder x ratio, stick-breaking
simplexes (composition, BEAR weights), ladder first rung + gaps, base + per-bucket offsets. Then deterministic repair (clip, snap to
the register step), then the app's own validators (analyst.common.validate, risk.common.validate); a rejection is `InvalidPoint`
and costs no simulation.
"""

import copy
import csv
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.analyst import common as analyst_common
from app.risk import common as risk_common

SCHEMA_VERSION = "1.0.0"
REGIMES = ("BULL", "TREND", "WEAK", "BEAR")
FROZEN_CLASSES = ("risk-limit", "model-input", "regulatory", "design", "structural")
BEAR_KEYS = ("mom20", "mom63", "hit20", "vol20", "dd63")
STRATEGY_FIELDS = ("top_n", "lookback", "stock_trend_ma")
COMPOSITION_STEP = 0.01


class InvalidPoint(ValueError):
    """A point the structural encoding, the repair or the app's validators reject (no simulation was run)."""


# --- dotted paths into the app configs ("risk.sizing.minNewOrderInr", "risk.ladder.levels.0.drawdownPct") -----------------------------
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


# --- simplex by stick-breaking ----------------------------------------------------------------------------------------------------
def simplex_from_u(u: list[float], floor: float) -> list[float]:
    """k = len(u) + 1 weights, each at least `floor`, summing to 1, from k - 1 numbers in [0, 1]."""
    rest, share = 1.0, []
    for x in u:
        share.append(rest * x)
        rest *= 1.0 - x
    share.append(rest)
    k = len(share)
    return [floor + (1.0 - k * floor) * s for s in share]


def u_from_simplex(w: list[float], floor: float) -> list[float]:
    """The inverse of simplex_from_u (the live weights become the default point)."""
    k = len(w)
    share = [(x - floor) / (1.0 - k * floor) for x in w]
    out, used = [], 0.0
    for s in share[:-1]:
        left = 1.0 - used
        out.append(min(1.0, max(0.0, s / left)) if left > 1e-12 else 0.0)
        used += s
    return out


@dataclass(frozen=True)
class Dim:
    """One search dimension, as the sampler sees it."""
    name: str
    kind: str  # int, float or bool
    low: float | None = None
    high: float | None = None
    step: float | None = None  # the repair grid (the register's step); the sampler uses it only when the scale is not log
    log: bool = False
    cls: str = "tunable"
    group: str = ""
    stage: int = 2
    affects: str = "both"
    role: str = "set"
    arg: str = ""
    note: str = ""
    src: str = ""

    def repair(self, x):
        """Deterministic repair: cast, clip into the bounds, snap to the step grid."""
        if self.kind == "bool":
            return bool(round(x)) if not isinstance(x, bool) else x
        x = min(max(float(x), self.low), self.high)
        if self.step:
            x = self.low + round((x - self.low) / self.step) * self.step
            x = min(max(x, self.low), self.high)
        return int(round(x)) if self.kind == "int" else round(x, 10)

    def sampler_step(self):
        return None if self.log or self.kind == "bool" else self.step

    def spec(self) -> dict:
        """What a sampler adapter needs: kind, bounds, step and scale."""
        return {"kind": self.kind, "low": self.low, "high": self.high, "step": self.sampler_step(), "log": self.log}


# --- schema generation from the register -----------------------------------------------------------------------------------------
BOUND = re.compile(r"(?:proposed )?(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)(?: step (-?\d+(?:\.\d+)?))?")
REL = re.compile(r"proposed \+-(\d+)% of the default")


def _num(text: str):
    v = float(text)
    return int(v) if v == int(v) and "." not in text else v


def parse_bounds(row: dict) -> tuple | None:
    """(low, high, step) from a register row, None when the row has no numeric bounds."""
    m = BOUND.fullmatch(row["bounds"].split(" (")[0].strip())
    if m:
        return _num(m.group(1)), _num(m.group(2)), _num(m.group(3)) if m.group(3) else None
    return None


def _register(path: Path) -> dict:
    with open(path, encoding="utf-8", newline="") as f:
        return {r["path"]: r for r in csv.DictReader(f)}


def _name(path: str) -> str:
    return path.replace(":", ".", 1)


def build_schema(register_path: str | Path, extra_path: str | Path, risk: dict, analyst: dict, settings: dict) -> dict:
    """The schema document: one entry per search dimension, generated from the register rows of class tunable and risk-limit."""
    rows, extra = _register(Path(register_path)), json.loads(Path(extra_path).read_text(encoding="utf-8"))
    log_keys, stages, stage_zero = extra["log"], extra["stages"], set(extra["stageZero"])
    buckets = list(analyst["composition"])
    dims: list[Dim] = []

    def add(name, kind, bounds, cls, group, role="set", arg="", note="", src="", affects="both", log=None):
        low, high, step = bounds if bounds else (None, None, None)
        is_log = any(k in name for k in log_keys) if log is None else log
        dims.append(Dim(name, kind, low, high, step, bool(is_log and kind != "bool" and low and low > 0), cls, group,
                        0 if name in stage_zero else stages.get(group, 2), affects, role, arg, note, src))

    def need(path: str) -> tuple:
        b = parse_bounds(rows[path])
        if not b:
            raise ValueError(f"register row {path} has no numeric bounds: add bounds to {extra_path}")
        return b

    def region(path: str) -> tuple:
        return parse_bounds(rows[path]) or (0, 1, None)

    slow_bounds = None
    for path, r in rows.items():
        if r["class"] not in ("tunable", "risk-limit") or r["kind"] == "code" or path.split(":")[0] not in ("risk", "analyst"):
            continue
        name, cls, group, affects, note = _name(path), r["class"], r["group"], r["affects"], r["note"]
        target, rest = path.split(":")[0], path.split(":")[1]
        if rest.startswith("strategies.") and rest.split(".")[-1] in STRATEGY_FIELDS:
            continue  # handled below, once per regime and field
        if rest == "sizing.minAdjustmentInr":
            add("risk.sizing.minAdjRatio", "float", tuple(extra["bounds"]["risk:sizing.minAdjRatio"].values()) + (None,), cls, group, "minAdjRatio", note="minAdjustmentInr = minNewOrderInr x ratio (never above it)", src=path)
        elif rest.startswith("stops.clampPct."):
            b, lo, wd = rest.split(".")[-1], extra["bounds"]["risk:stops.clampLo"], extra["bounds"]["risk:stops.clampWidth"]
            add(f"risk.stops.clampLo.{b}", "float", (lo["low"], lo["high"], lo["step"]), cls, group, "clampLo", b, "lower stop clamp", path)
            add(f"risk.stops.clampWidth.{b}", "float", (wd["low"], wd["high"], wd["step"]), cls, group, "clampWidth", b, "upper clamp = lo + width", path)
        elif rest == "regime.smaSlow":
            fast, slow = need("analyst:regime.smaFast"), need(path)
            slow_bounds = [slow[0], slow[1]]
            add("analyst.regime.smaGap", "int", (slow[0] - fast[1], slow[1] - fast[0], math.gcd(int(fast[2] or 1), int(slow[2] or 1))), cls, group, "smaGap",
                note="smaSlow = smaFast + smaGap, clipped into the register bounds of smaSlow", src=path, affects=affects)
        elif rest.startswith("composition."):
            if rest.endswith(buckets[0]):
                for i in range(len(buckets) - 1):
                    add(f"analyst.composition.u{i + 1}", "float", (0.0, 1.0, None), cls, group, "compU", str(i), "stick-breaking coordinate of the bucket weights", path)
        elif rest.startswith("selector.bearScore.") and rest.split(".")[-1] in BEAR_KEYS:
            if rest.endswith(BEAR_KEYS[0]):
                for i in range(len(BEAR_KEYS) - 1):
                    add(f"analyst.selector.bearScore.u{i + 1}", "float", (0.0, 1.0, None), cls, group, "bearU", str(i), "stick-breaking coordinate of the L1-normalised BEAR weights", path)
        elif rest.startswith("ladder.levels.") and rest.endswith(".drawdownPct"):
            i = int(rest.split(".")[2])
            b = need(path)
            if i == 0:
                add("risk.ladder.levels.0.drawdownPct", "float", b, cls, group, "ladderFirst", note="first rung; later rungs are this plus gaps", src=path)
            else:
                prev, g = need(f"risk:ladder.levels.{i - 1}.drawdownPct"), extra["bounds"]["risk:ladder.gap"]
                add(f"risk.ladder.levels.{i}.drawdownGap", "float", (g["low"], round(b[1] - prev[0], 10), g["step"]), cls, group, "ladderGap", str(i),
                    f"rung {i + 1} = rung {i} + gap", path)
        elif r["kind"] == "bool":
            add(name, "bool", None, cls, group, src=path, note=note, affects=affects)
        elif r["kind"] in ("int", "float"):
            b = parse_bounds(r)
            m = REL.fullmatch(r["bounds"])
            if b is None and m:  # +-x% of the default: resolved against the live value
                b = (-float(m.group(1)) / 100, float(m.group(1)) / 100, None)
                add(name, r["kind"], b, cls, group, "setRel", note=note, src=path, affects=affects)
                continue
            if b is None:
                b = extra["bounds"].get(path)
                if b is None:
                    if path in rows and not r["bounds"]:
                        continue  # a risk-limit row with no bounds (the last ladder rung's 0) is not searchable
                    raise ValueError(f"register row {path} has no numeric bounds: add bounds to {extra_path}")
                b = (b["low"], b["high"], b.get("step"))
            add(name, r["kind"], b, cls, group, src=path, note=note, affects=affects)

    # per regime and field: a shared base (the first bucket's value) and offsets for the other buckets
    share = settings["offsetShare"]
    for regime in REGIMES:
        for f in STRATEGY_FIELDS:
            first = f"analyst:strategies.{regime}.{buckets[0]}.{f}"
            b = need(first)
            for other in buckets[1:]:
                if need(f"analyst:strategies.{regime}.{other}.{f}") != b:
                    raise ValueError(f"register: {regime}.{f} bounds differ between buckets; the offset encoding needs one range")
            cells = round((b[1] - b[0]) / (b[2] or 1))
            k = max(1, math.ceil(share * cells))
            base = f"analyst.strategies.{regime}.{f}"
            add(base, "int", b, rows[first]["class"], "selector", "stratBase", f"{regime}.{f}", f"shared value of {f} in {regime}", first)
            for other in buckets[1:]:
                add(f"{base}.off.{other}", "int", (-k, k, 1), rows[first]["class"], "selector", "stratOff", f"{regime}.{f}.{other}", f"{other} offset from the shared value, in grid steps", first, log=False)

    names = [d.name for d in dims]
    if len(set(names)) != len(names):
        raise ValueError("schema: duplicate dimension names")
    return {"schemaVersion": SCHEMA_VERSION, "registerSha": hashlib.sha256(Path(register_path).read_bytes()).hexdigest()[:16],
            "extraSha": hashlib.sha256(Path(extra_path).read_bytes()).hexdigest()[:16], "settings": {"offsetShare": share},
            "slowBounds": slow_bounds, "dims": [{k: v for k, v in asdict(d).items() if not (v is None or v == "" or (k == "log" and not v))} for d in dims]}


def write_schema(doc: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


class ParameterSpace:
    """The schema over the live configs: defaults, widened bounds, selection of active dimensions, decode."""

    def __init__(self, schema: dict, risk: dict, analyst: dict, settings: dict):
        self.schema, self.version, self.base_risk, self.base_analyst = schema, schema["schemaVersion"], risk, analyst
        self.floor = settings["compositionFloor"]
        self.buckets = list(analyst["composition"])
        self.bear_sign = {k: (-1.0 if analyst["selector"]["bearScore"][k] < 0 else 1.0) for k in BEAR_KEYS}
        self.bear_scale = sum(abs(analyst["selector"]["bearScore"][k]) for k in BEAR_KEYS)
        self.widened: list[dict] = []
        raw = [Dim(**d) for d in schema["dims"]]
        self.defaults: dict = {}
        self.dims: dict[str, Dim] = {}
        for d in raw:
            default = self._live_default(d, raw)
            if d.role == "setRel":  # +-share of the live value
                lo, hi = round(default * (1 + d.low)), round(default * (1 + d.high))
                d = replace(d, low=max(lo, 2), high=max(hi, max(lo, 2) + 1), step=1)
            self.dims[d.name] = self._widen(d, default)
            self.defaults[d.name] = default
        self.names = list(self.dims)
        self.decode(self.defaults)  # the live configuration must be a valid point of its own space

    # --- defaults: the live values, in the encoding of each dimension ---------------------------------------------------------------
    def _live_default(self, d: Dim, raw: list[Dim]):
        risk, analyst = self.base_risk, self.base_analyst
        if d.role in ("set", "setRel"):
            return get_path({"risk": risk, "analyst": analyst}, d.name.split("."))
        if d.role == "minAdjRatio":
            return round(risk["sizing"]["minAdjustmentInr"] / risk["sizing"]["minNewOrderInr"], 10)
        if d.role in ("clampLo", "clampWidth"):
            lo, hi = risk["stops"]["clampPct"][d.arg]
            return round(lo, 10) if d.role == "clampLo" else round(hi - lo, 10)
        if d.role == "smaGap":
            return analyst["regime"]["smaSlow"] - analyst["regime"]["smaFast"]
        if d.role == "compU":
            return round(u_from_simplex([analyst["composition"][b] for b in self.buckets], self.floor)[int(d.arg)], 10)
        if d.role == "bearU":
            mags = [abs(analyst["selector"]["bearScore"][k]) / self.bear_scale for k in BEAR_KEYS]
            return round(u_from_simplex(mags, 0.0)[int(d.arg)], 10)
        if d.role == "ladderFirst":
            return risk["ladder"]["levels"][0]["drawdownPct"]
        if d.role == "ladderGap":
            i = int(d.arg)
            lv = risk["ladder"]["levels"]
            return round(lv[i]["drawdownPct"] - lv[i - 1]["drawdownPct"], 10)
        if d.role == "stratBase":
            regime, f = d.arg.split(".")
            return analyst["strategies"][regime][self.buckets[0]][f]
        if d.role == "stratOff":
            regime, f, bucket = d.arg.split(".")
            base = next(x for x in raw if x.role == "stratBase" and x.arg == f"{regime}.{f}")
            step = base.step or 1
            return round((analyst["strategies"][regime][bucket][f] - analyst["strategies"][regime][self.buckets[0]][f]) / step)
        raise ValueError(f"schema: unknown role {d.role} for {d.name}")

    def _widen(self, d: Dim, default) -> Dim:
        """A bound that excludes the live value is widened to it (and the grid kept), so trial 0 is a point of the space."""
        if d.kind == "bool":
            return d
        low, high = d.low, d.high
        if default < low:
            low = default
        if default > high:
            high = default
        if d.step:
            high = low + math.ceil(round((high - low) / d.step, 9)) * d.step
        high = int(high) if d.kind == "int" else round(high, 10)
        low = int(low) if d.kind == "int" else round(low, 10)
        if (low, high) != (d.low, d.high):
            self.widened.append({"name": d.name, "register": [d.low, d.high], "space": [low, high], "live": default})
        return replace(d, low=low, high=high)

    # --- selection ------------------------------------------------------------------------------------------------------------------
    def tokens(self, token: str) -> list[str]:
        """Dimension names a study-file token stands for: a name, `group:<g>`, `class:<c>`, `stage:<n>`, `prefix:<p>`, `all`."""
        if token == "all":
            return list(self.names)
        kind, _, val = token.partition(":")
        if val and kind == "group":
            out = [n for n, d in self.dims.items() if d.group == val]
        elif val and kind == "class":
            out = [n for n, d in self.dims.items() if d.cls == val]
        elif val and kind == "stage":
            out = [n for n, d in self.dims.items() if str(d.stage) == val]
        elif val and kind == "prefix":
            out = [n for n in self.names if n.startswith(val)]
        else:
            out = [token] if token in self.dims else []
        if not out:
            raise ValueError(f"study: '{token}' matches no parameter (see `space show`)")
        return out

    def select(self, active: list[str], exclude: list[str] = (), allow_unfreeze: list[str] = ()) -> list[str]:
        """The active dimensions of a study. Frozen classes need an explicit `allow_unfreeze`; a bool or non-tunable one without bounds cannot be searched."""
        chosen: list[str] = []
        for t in active:
            chosen += [n for n in self.tokens(t) if n not in chosen]
        drop = {n for t in exclude for n in self.tokens(t)}
        allowed = {n for t in allow_unfreeze for n in self.tokens(t)}
        out = []
        for n in chosen:
            if n in drop:
                continue
            d = self.dims[n]
            if d.cls in FROZEN_CLASSES and n not in allowed:
                continue  # frozen by default: only a listed allow_unfreeze turns it on
            out.append(n)
        return out

    def unfrozen(self, active: list[str]) -> list[str]:
        """Active dimensions of a frozen class (every report prints them as a warning)."""
        return [n for n in active if self.dims[n].cls in FROZEN_CLASSES]

    # --- decode ---------------------------------------------------------------------------------------------------------------------
    def complete(self, values: dict) -> dict:
        """Every dimension, repaired; a dimension the point does not name takes its live value."""
        unknown = set(values) - set(self.dims)
        if unknown:
            raise InvalidPoint(f"unknown parameters: {sorted(unknown)}")
        return {n: self.dims[n].repair(values.get(n, self.defaults[n])) for n in self.names}

    def key(self, values: dict) -> str:
        return json.dumps(self.complete(values), sort_keys=True, separators=(",", ":"))

    def decode(self, values: dict) -> tuple[dict, dict, dict]:
        """(risk, analyst, full point): app configs for a point, validated by the app's own rules. Raises InvalidPoint."""
        v = self.complete(values)
        cfg = {"risk": copy.deepcopy(self.base_risk), "analyst": copy.deepcopy(self.base_analyst)}
        risk, analyst = cfg["risk"], cfg["analyst"]
        try:
            self._write(v, cfg)
            r = analyst["regime"]
            r["minRows"] = max(r["minRows"], max(r["smaSlow"], r["momentumDays"]) + r.get("unknownExtra", 9) + 1)  # data sufficiency follows the windows
            analyst_common.validate(analyst)
            risk_common.validate(risk, "run")
        except (ValueError, KeyError, ZeroDivisionError) as e:
            raise InvalidPoint(str(e)) from e
        return risk, analyst, v

    def _write(self, v: dict, cfg: dict) -> None:
        risk, analyst = cfg["risk"], cfg["analyst"]
        sizing, stops, lad, reg = risk["sizing"], risk["stops"], risk["ladder"], analyst["regime"]
        roles: dict[str, list[Dim]] = {}
        for d in self.dims.values():
            roles.setdefault(d.role, []).append(d)
        for d in roles.get("set", []):
            set_path(cfg, d.name.split("."), v[d.name])
        for d in roles.get("setRel", []):
            set_path(cfg, d.name.split("."), v[d.name])
        for d in roles.get("minAdjRatio", []):
            sizing["minAdjustmentInr"] = max(1, round(sizing["minNewOrderInr"] * v[d.name] / 100) * 100)  # whole hundreds of rupees, never above minNewOrderInr
            sizing["minAdjustmentInr"] = min(sizing["minAdjustmentInr"], sizing["minNewOrderInr"])
        for d in roles.get("clampLo", []):
            lo = v[d.name]
            stops["clampPct"][d.arg] = [lo, round(lo + v[f"risk.stops.clampWidth.{d.arg}"], 10)]
        if "smaGap" in {d.role for d in self.dims.values()}:
            slow = next(d for d in self.dims.values() if d.role == "smaGap")
            lo, hi = self._slow_bounds()
            reg["smaSlow"] = int(min(max(v["analyst.regime.smaFast"] + v[slow.name], lo), hi))
            reg["smaFast"] = v["analyst.regime.smaFast"]
        cu = [v[f"analyst.composition.u{i + 1}"] for i in range(len(self.buckets) - 1)] if roles.get("compU") else None
        if cu is not None:
            w = [round(x / COMPOSITION_STEP) * COMPOSITION_STEP for x in simplex_from_u(cu, self.floor)]
            w = [round(x, 10) for x in w[:-1]]
            w.append(round(1.0 - sum(w), 10))
            if min(w) < 0:
                raise ValueError("composition rounds to a negative weight")
            analyst["composition"] = dict(zip(self.buckets, w))
        bu = [v[f"analyst.selector.bearScore.u{i + 1}"] for i in range(len(BEAR_KEYS) - 1)] if roles.get("bearU") else None
        if bu is not None:
            mags = simplex_from_u(bu, 0.0)
            bear = analyst["selector"]["bearScore"]
            for k, m in zip(BEAR_KEYS, mags):
                bear[k] = round(self.bear_sign[k] * m * self.bear_scale, 10)
        for d in roles.get("ladderFirst", []):
            self._write_ladder(v, lad)
        for d in roles.get("stratBase", []):
            regime, f = d.arg.split(".")
            for i, b in enumerate(self.buckets):
                off = v[f"{d.name}.off.{b}"] if i else 0
                analyst["strategies"][regime][b][f] = d.repair(v[d.name] + off * (d.step or 1))

    def _slow_bounds(self) -> tuple[int, int]:
        """The register bounds of smaSlow (stored on the smaGap dimension's source row, read back from the schema)."""
        s = self.schema.get("slowBounds")
        return (s[0], s[1]) if s else (0, 10**6)

    def _write_ladder(self, v: dict, lad: dict) -> None:
        levels = lad["levels"]
        dd = v["risk.ladder.levels.0.drawdownPct"]
        levels[0]["drawdownPct"] = dd
        for i in range(1, len(levels)):
            g = v.get(f"risk.ladder.levels.{i}.drawdownGap")
            if g is None:
                break
            dd = round(dd + g, 10)
            levels[i]["drawdownPct"] = dd

    # --- facts the evaluation needs ------------------------------------------------------------------------------------------------
    def longest_lookback(self) -> int:
        """Longest look-back any allowed point can use, in trading days, over the whole space (so folds never move between studies)."""
        longest = [self.dims[n].high for n in self.names if re.search(r"\.(lookback|stock_trend_ma)$|regime\.(smaFast|momentumDays)$", n)]
        slow = self._slow_bounds()[1]
        longest.append(slow if slow < 10**6 else self.base_analyst["regime"]["smaSlow"])
        return int(max(longest))

    def warmup_rows(self) -> int:
        """Index rows before the slowest allowed regime is known, plus the longest allowed persistence (5 sessions a week)."""
        reg = self.base_analyst["regime"]
        slow = self._slow_bounds()[1] if self._slow_bounds()[1] < 10**6 else reg["smaSlow"]
        mom = self.dims["analyst.regime.momentumDays"].high if "analyst.regime.momentumDays" in self.dims else reg["momentumDays"]
        extra = self.dims["analyst.regime.unknownExtra"].high if "analyst.regime.unknownExtra" in self.dims else reg.get("unknownExtra", 9)
        pers = self.dims["analyst.regime.persistenceWeeks"].high if "analyst.regime.persistenceWeeks" in self.dims else reg["persistenceWeeks"]
        return int(max(slow, mom) + extra + 5 * pers)

    def describe(self, names: list[str] | None = None) -> list[dict]:
        rows = []
        for n in names or self.names:
            d = self.dims[n]
            rows.append({"name": n, "kind": d.kind, "default": self.defaults[n], "low": d.low, "high": d.high, "step": d.step, "scale": "log" if d.log else "linear",
                         "class": d.cls, "group": d.group, "stage": d.stage, "role": d.role, "note": d.note})
        return rows


def load(settings: dict, risk: dict, analyst: dict, schema_path: str | Path | None = None) -> ParameterSpace:
    schema = json.loads(Path(schema_path or settings["paths"]["schema"]).read_text(encoding="utf-8"))
    return ParameterSpace(schema, risk, analyst, settings["space"])
