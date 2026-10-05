"""Everything a run needs, built once from the app configs, the backtest config and the stored data.

The app's config files are read as the base and edited in memory by backtest.json "overrides"; nothing is written back.
"""

import copy
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.analyst import common as analyst_common
from app.risk import common as risk_common
from backtest import params, prep, surv_proxy
from backtest.dividends import Dividends
from backtest.pit import PitData
from backtest.targets import Targets


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def app_configs(cfg: dict) -> tuple[dict, dict]:
    """(risk config, analyst config): the live files with the backtest overrides applied and validated."""
    analyst = deep_merge(analyst_common.load_config(), cfg["overrides"]["analyst"])
    analyst_common.validate(analyst)
    risk = deep_merge(risk_common.load_config("run"), cfg["overrides"]["risk"])
    risk_common.validate(risk, "run")
    if cfg["capital"]["composition"] != analyst["composition"]:
        raise ValueError("backtest capital.composition must equal the analyst composition (set it under overrides.analyst to change both)")
    return risk, analyst


def code_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def config_hash(cfg: dict, risk: dict, analyst: dict) -> str:
    """Hash of everything that decides a result (paths excluded: they say where, not what)."""
    blob = json.dumps({"backtest": cfg, "risk": {k: v for k, v in risk.items() if k != "paths"},
                       "analyst": {k: v for k, v in analyst.items() if k != "paths"}}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class World:
    cfg: dict
    risk: dict
    analyst: dict
    data: PitData
    targets: Targets
    dividends: Dividends
    surveillance: object
    cache: dict = field(default_factory=dict, compare=False, repr=False)  # api.py: targets cache and run hashes

    @classmethod
    def build(cls, cfg: dict) -> "World":
        risk, analyst = app_configs(cfg)
        data = prep.load_pit(cfg)
        return cls(cfg, risk, analyst, data, Targets(data, analyst), Dividends.load(Path(cfg["paths"]["data"]) / "dividends.csv"),
                   surv_proxy.surveillance_for(data, cfg))

    def first_known_regime(self) -> str:
        """First rebalance date whose active regime is known (the earliest day anything can be selected)."""
        h = self.targets.history
        return str(h[h.active_regime != "Unknown"].date.iloc[0])


def parse_set(items: list[str]) -> dict:
    """--set key=value pairs (numbers) -> a partial parameter point."""
    out = {}
    for item in items:
        key, _, raw = item.partition("=")
        if not raw:
            raise ValueError(f"--set expects key=value, got {item!r}")
        out[key] = int(raw) if raw.lstrip("-").isdigit() else float(raw)
    return out


def with_point(w: World, point: dict) -> World:
    """The world with a parameter point from params.json applied (identity when the point is empty)."""
    if not point:
        return w
    risk, analyst = params.Schema.load(w.cfg["paths"]["params"], w.risk, w.analyst).apply(point)
    return World(w.cfg, risk, analyst, w.data, Targets(w.data, analyst), w.dividends, w.surveillance)
