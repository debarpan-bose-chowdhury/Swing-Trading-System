"""Stage logic: the B blocks, the switch from B to C, and chaining a study onto the winner of the one before.

Stage B runs in blocks over the kept dimensions: signal (register stage 1), stops and sizing (2), regime and composition (3). Each block fixes
the dimensions of the earlier blocks at the selected front trial of that block (`from:` in the study file), runs once, and adds to the
effective-N ledger like any other study; an extra cycle is a deliberate act. Stage C refines at most `maxActive` dimensions with the GP sampler
once the hypervolume has stopped growing (under 1% over 150 trials) and the importance is concentrated; it stays on TPE when more than 30% of
the active dimensions are categorical (booleans here).
"""

from hpo import pareto
from hpo.errors import Failed

BLOCKS = [("B1", "signal", {1}), ("B2", "stops and sizing", {0, 2}), ("B3", "regime and composition", {3})]
HV_WINDOW, HV_GAIN, CATEGORICAL_MAX = 150, 0.01, 0.30


def front_rows(records: list[dict]) -> list[dict]:
    """The feasible front of a study as {"trial", "f1", "f2", "calmar", "values"} rows (f2 is the drawdown depth)."""
    feas = [r for r in records if r["feasible"]]
    return [{"trial": feas[i]["trial"], "f1": feas[i]["objectives"][0], "f2": feas[i]["objectives"][1], "calmar": feas[i]["metrics"].get("calmar"),
             "fills": feas[i]["metrics"].get("fills"), "values": feas[i]["values"]} for i in pareto.front([tuple(r["objectives"]) for r in feas])]


def hv_gain(records: list[dict], ref: tuple[float, float], window: int = HV_WINDOW) -> float | None:
    """Relative hypervolume gain of the last `window` trials, None while there are not enough trials or no front yet."""
    if len(records) <= window:
        return None
    pts = lambda rs: [tuple(r["objectives"]) for r in rs if r["feasible"]]  # noqa: E731
    before = pareto.hypervolume(pts(records[:-window]), ref)
    return None if before <= 0 else pareto.hypervolume(pts(records), ref) / before - 1.0


def recommend(records: list[dict], spec: dict, space, sens: dict | None, cfg: dict) -> dict:
    """Should the study move from TPE (B) to GP refinement (C)?"""
    gain = hv_gain(records, tuple(cfg["objectives"]["hvReference"]))
    kept = sens["keep"] if sens else None
    cat = sum(space.dims[n].kind == "bool" for n in spec["active"]) / max(1, len(spec["active"]))
    reasons = []
    if gain is None:
        reasons.append(f"fewer than {HV_WINDOW + 1} trials, or no feasible front yet")
    elif gain >= HV_GAIN:
        reasons.append(f"hypervolume still growing ({gain:.1%} over the last {HV_WINDOW} trials)")
    if kept is None:
        reasons.append("no sensitivity analysis yet (run `sensitivity`)")
    elif len(kept) > cfg["sensitivity"]["maxActive"]:
        reasons.append(f"importance spread over {len(kept)} parameters (more than {cfg['sensitivity']['maxActive']})")
    if cat > CATEGORICAL_MAX:
        return {"action": "stay on tpe", "hvGain": gain, "categoricalShare": cat, "reasons": [f"{cat:.0%} of the active parameters are categorical (limit {CATEGORICAL_MAX:.0%})"]}
    return {"action": "switch to gp" if not reasons else "stay on tpe", "hvGain": gain, "categoricalShare": cat, "reasons": reasons}


def chain_fixed(prev_spec: dict, prev_records: list[dict], rule: str) -> dict:
    """Fixed values for the next block: the earlier fixed ones plus the selected front trial's values of the dimensions the earlier study searched."""
    rows = front_rows(prev_records)
    if not rows:
        raise Failed(f"study {prev_spec['name']} has no feasible trial: nothing to carry into the next block")
    best = pareto.select(rows, rule)
    return {**prev_spec.get("fixed", {}), **{n: best["values"][n] for n in prev_spec["active"]}}


def plan_blocks(space, sens: dict, spec: dict, trials: int = 400) -> list[dict]:
    """The B1..B3 study files for the kept dimensions of a Stage A screen (blocks without kept dimensions are skipped)."""
    out, prev = [], None
    for tag, label, stages in BLOCKS:
        names = [n for n in sens["keep"] if space.dims[n].stage in stages]
        if not names:
            continue
        name = f"{spec['name']}-{tag}"
        out.append({"name": name, "stage": tag, "sampler": "tpe", "active": names, "trials": trials, "seed": spec["seed"], **({"from": prev} if prev else {}), "note": label})
        prev = name
    return out


def plan_refine(space, sens: dict, spec: dict, cfg: dict, trials: int = 150) -> dict:
    """The Stage C study: the most important dimensions (at most maxActive), GP sampler, fixed at the selected trial of the study it follows."""
    names = [r["name"] for r in sens["dims"] if r["name"] in spec["active"] or r["decision"] == "keep"][:cfg["sensitivity"]["maxActive"]]
    return {"name": f"{spec['name']}-C", "stage": "C", "sampler": "gp", "active": names, "trials": trials, "seed": spec["seed"], "from": spec["name"]}
