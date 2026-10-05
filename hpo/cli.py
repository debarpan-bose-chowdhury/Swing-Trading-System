"""python -m hpo.cli <command>. Run from the repo root. Exit codes: 0 ok, 1 failed, 2 busy (run lock), 3 refused (cap, changed inputs, gate, holdout).

  --check                         validate hpo.json, the schema and the imports (no network, no writes)
  space build|check|show [--stage N]
  study new --config F | run --name N [--trials T] [--workers W] [--override-cap "reason"] | resume --name N | status --name N
  sensitivity --name N            importance and the freeze list (writes sensitivity.json and proposed_active.yaml in the study folder)
  report --name N [--open]        HTML study report;  live --name N  auto-refreshing dashboard while a study runs
  ledger show
  front, robust, gate, holdout, promote: later phases (they exit 1 with a message until built)
"""

import argparse
import functools
import json
import sys
import time
import webbrowser
from pathlib import Path

import yaml

from hpo import ledger as ledger_mod
from hpo import objective, sensitivity, settings, space as space_mod, study as study_mod
from hpo.errors import Busy, Failed, Refusal
from hpo.status import read_json, write_json

LATER = {"front": 3, "robust": 4, "gate": 4, "holdout": 5, "promote": 5}


def _space(cfg: dict):
    from backtest import api
    bt, risk, analyst = api.base_configs({"universe": {"mode": cfg["universe"]["selection"]}})
    return bt, space_mod.load(cfg, risk, analyst)


def _table(rows: list[dict], cols: list[str]) -> str:
    width = {c: max(len(c), *(len(f"{r[c]:.4g}" if isinstance(r[c], float) else str(r[c])) for r in rows)) if rows else len(c) for c in cols}
    fmt = lambda v: f"{v:.4g}" if isinstance(v, float) else str(v)  # noqa: E731
    return "\n".join(["  ".join(c.ljust(width[c]) for c in cols), *("  ".join(fmt(r[c]).ljust(width[c]) for c in cols) for r in rows)])


def cmd_space(a, cfg) -> int:
    bt, sp = _space(cfg)
    if a.action == "build":
        from backtest import api
        _, risk, analyst = api.base_configs()
        space_mod.write_schema(space_mod.build_schema(cfg["paths"]["register"], cfg["paths"]["extraBounds"], risk, analyst, cfg["space"]), cfg["paths"]["schema"])
        print(f"wrote {cfg['paths']['schema']}")
        return 0
    if a.action == "check":
        fresh = space_mod.build_schema(cfg["paths"]["register"], cfg["paths"]["extraBounds"], sp.base_risk, sp.base_analyst, cfg["space"])
        stale = fresh != sp.schema
        print(f"{len(sp.names)} dimensions, schema {sp.version}, {'STALE: run `space build`' if stale else 'in step with the register'}")
        for w in sp.widened:
            print(f"  widened to include the live value: {w['name']} register {w['register']} -> {w['space']} (live {w['live']})")
        by = {}
        for d in sp.dims.values():
            by[(d.cls, d.stage)] = by.get((d.cls, d.stage), 0) + 1
        print("  by (class, stage):", ", ".join(f"{k[0]}/{k[1]}: {v}" for k, v in sorted(by.items())))
        return 3 if stale else 0
    names = sp.select(["all"], allow_unfreeze=["class:risk-limit"]) if a.stage is None else sp.select([f"stage:{a.stage}"])
    print(_table(sp.describe(names), ["name", "kind", "default", "low", "high", "step", "scale", "class", "stage"]))
    return 0


def cmd_study(a, cfg) -> int:
    bt, sp = _space(cfg)
    if a.action == "new":
        raw = study_mod.load_spec_file(a.config)
        st = study_mod.Study.create(cfg, sp, raw)
        print(f"created study {st.spec['name']}: {len(st.spec['active'])} active parameters, {st.spec['trials']} trials, sampler {st.spec['sampler']}, folder {st.dir}")
        for n in st.spec["unfrozen"]:
            print(f"  WARNING frozen-class parameter searched: {n}")
        return 0
    if a.action == "status":
        s = read_json(Path(cfg["paths"]["data"]) / "studies" / a.name / "status.json")
        if s is None:
            print(f"no status for {a.name}")
            return 1
        from hpo.progress import line
        print(line(s) + f"  [{s['state']}, updated {s['updatedAt']}]")
        return 0
    st = study_mod.Study.open(cfg, sp, a.name)
    factory = functools.partial(objective.BacktestRunner, cfg)
    res = st.run(factory, trials=getattr(a, "trials", None), workers=getattr(a, "workers", None), override_cap=getattr(a, "override_cap", None), bt_cfg=bt)
    return 0 if res["state"] in ("finished", "stopped") else 1


def cmd_sensitivity(a, cfg) -> int:
    _, sp = _space(cfg)
    st = study_mod.Study.open(cfg, sp, a.name)
    res = sensitivity.analyse(sp, st.spec, study_mod.read_records(st.trials_path), cfg)
    write_json(st.dir / "sensitivity.json", res)
    (st.dir / "proposed_active.yaml").write_text(yaml.safe_dump(res["proposedStudy"], sort_keys=False), encoding="utf-8")
    print(_table([{**r, "name": r["name"][-48:]} for r in res["dims"]], ["name", "importanceF1", "importanceF2", "importanceFeasibility", "spearmanF1", "decision"]))
    print(f"\nkeep {len(res['keep'])}, freeze {len(res['freeze'])} ({len(res['collapsedOffsets'])} per-bucket offsets collapse to the shared value); "
          f"proposed next study: {st.dir / 'proposed_active.yaml'}")
    return 0


def cmd_report(a, cfg) -> int:
    from hpo.viz import report
    if getattr(a, "candidate", None):
        print("candidate reports (charts 11-24) arrive with the robustness phase")
        return 1
    out = report.study_report(Path(cfg["paths"]["data"]) / "studies" / a.name, cfg)
    print(out)
    if a.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_live(a, cfg) -> int:
    from hpo.viz import report
    folder = Path(cfg["paths"]["data"]) / "studies" / a.name
    print(f"{folder / 'report' / 'live.html'} refreshes every {cfg['compute']['liveRefreshSeconds']} s; Ctrl-C stops")
    opened = False
    try:
        while True:
            out = report.study_report(folder, cfg, live=True)
            if a.open and not opened:
                webbrowser.open(out.resolve().as_uri())
                opened = True
            time.sleep(cfg["compute"]["liveRefreshSeconds"])
    except KeyboardInterrupt:
        return 0


def cmd_ledger(a, cfg) -> int:
    led = ledger_mod.Ledger(Path(cfg["paths"]["data"]) / "ledger" / "research_ledger.json", cfg["ledger"]["effectiveNCap"], cfg["ledger"]["defaultEffectiveRatio"])
    print(f"effective N {led.effective_total()} of cap {led.cap}; raw {led.raw_total()}; ratio {led.ratio():.2f}")
    for n, s in led.doc["studies"].items():
        print(f"  {n}: stage {s['stage']}, raw {s['raw']}, effective {s['effective']} ({s['at']})")
    for o in led.doc["overrides"]:
        print(f"  override {o['study']}: {o['reason']} ({o['at']})")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m hpo.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("space")
    s.add_argument("action", choices=["build", "check", "show"])
    s.add_argument("--stage", type=int)
    st = sub.add_parser("study")
    ssub = st.add_subparsers(dest="action", required=True)
    n = ssub.add_parser("new")
    n.add_argument("--config", required=True)
    r = ssub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--trials", type=int)
    r.add_argument("--workers", type=int)
    r.add_argument("--override-cap", dest="override_cap")
    ssub.add_parser("resume").add_argument("--name", required=True)
    ssub.add_parser("status").add_argument("--name", required=True)
    sub.add_parser("sensitivity").add_argument("--name", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--name")
    rep.add_argument("--candidate")
    rep.add_argument("--open", action="store_true")
    lv = sub.add_parser("live")
    lv.add_argument("--name", required=True)
    lv.add_argument("--open", action="store_true")
    lg = sub.add_parser("ledger")
    lg.add_argument("action", choices=["show"])
    for name in LATER:
        sub.add_parser(name).add_argument("rest", nargs="*")
    return p


def check(cfg: dict) -> int:
    bt, sp = _space(cfg)
    fresh = space_mod.build_schema(cfg["paths"]["register"], cfg["paths"]["extraBounds"], sp.base_risk, sp.base_analyst, cfg["space"])
    import optuna  # noqa: F401
    import plotly  # noqa: F401
    import scipy  # noqa: F401
    print(f"hpo.json ok; {len(sp.names)} dimensions; schema {'in step with' if fresh == sp.schema else 'STALE against'} the register; optuna {optuna.__version__}")
    return 0 if fresh == sp.schema else 3


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        cfg = settings.load()
        if a.check:
            return check(cfg)
        if a.cmd in LATER:
            print(f"`{a.cmd}` arrives in phase {LATER[a.cmd]} of doc/HPO_TDD.md")
            return 1
        handler = {"space": cmd_space, "study": cmd_study, "sensitivity": cmd_sensitivity, "report": cmd_report, "live": cmd_live, "ledger": cmd_ledger}.get(a.cmd)
        if handler is None:
            parser().print_help()
            return 1
        if a.cmd == "study" and a.action == "resume":
            a.trials = a.workers = a.override_cap = None
            a.action = "run"
        return handler(a, cfg)
    except Busy as e:
        print(f"busy: {e}", file=sys.stderr)
        return 2
    except Refusal as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    except (Failed, ValueError, FileNotFoundError) as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
