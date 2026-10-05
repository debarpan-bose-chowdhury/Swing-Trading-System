"""python -m hpo.cli <command>. Run from the repo root. Exit codes: 0 ok, 1 failed, 2 busy (run lock), 3 refused (cap, changed inputs, gate, holdout).

  --check                         validate hpo.json, the schema and the imports (no network, no writes)
  space build|check|show [--stage N]
  study new --config F | run --name N [--trials T] [--workers W] [--override-cap "reason"] | resume --name N | status --name N
  sensitivity --name N            importance and the freeze list (writes sensitivity.json and proposed_active.yaml in the study folder)
  report --name N | --candidate ID [--open]   HTML study report or candidate report;  live --name N  auto-refreshing dashboard while a study runs
  ledger show
  front --name N [--select calmar|knee]   the feasible front and one pick
  stages plan|advise --name N     B-block study files from a screen; the B-to-C switch advice (GP refinement file)
  robust --name N [--top 30]      neighbourhood-score the finalists (plateau, not peak); writes robust.json, prints the pick or 'keep defaults'
  gate --candidate ID             full evidence (stress, neighbours, grid, PBO/DSR/SPA, exploit audit) and the verdict; candidates/<id>/report.html (charts 11-24); exit 3 when not met
  holdout --candidate ID          the one-shot holdout (once, for one parameter set, against the criterion registered at gate time; exit 3 if refused or not passed)
  promote --candidate ID          overlay.json, rollback.json, diff.md, dossier for a candidate that passed gate, audit and holdout (never writes app/config)
  shadow check --candidate ID [--nav CSV] [--since DATE]   the 13-week shadow comparison against the limits in the dossier (exit 3 = roll back)
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
from hpo import gate as gate_mod, holdout as holdout_mod, objective, promote as promote_mod, shadow as shadow_mod, pareto, robust as robust_mod, sensitivity, settings, stages, space as space_mod, study as study_mod
from hpo.errors import Busy, Failed, Refusal
from hpo.evalpool import EvalPool
from hpo.status import RunLock, read_json, write_json

LATER = {}


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
        raw.pop("note", None)
        if raw.get("from") and not raw.get("fixed"):
            prev = study_mod.Study.open(cfg, sp, raw["from"])
            raw["fixed"] = stages.chain_fixed(prev.spec, study_mod.read_records(prev.trials_path), raw.pop("from_rule", "calmar"))
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


def cmd_front(a, cfg) -> int:
    _, sp = _space(cfg)
    st = study_mod.Study.open(cfg, sp, a.name)
    rows = stages.front_rows(study_mod.read_records(st.trials_path))
    if not rows:
        print("no feasible trial yet")
        return 1
    pick = pareto.select(rows, a.select)
    show = [{"trial": r["trial"], "cagr%": round(100 * r["f1"], 2), "maxDD%": round(-100 * r["f2"], 2), "calmar": r["calmar"] if r["calmar"] is not None else float("nan"), "fills": r["fills"],
             "picked": "<-- " + a.select if r["trial"] == pick["trial"] else ""} for r in sorted(rows, key=lambda r: r["f2"])]
    print(_table(show, ["trial", "cagr%", "maxDD%", "calmar", "fills", "picked"]))
    write_json(st.dir / "front.json", {"select": a.select, "picked": pick["trial"], "front": rows})
    return 0


def cmd_stages(a, cfg) -> int:
    _, sp = _space(cfg)
    st = study_mod.Study.open(cfg, sp, a.name)
    recs = study_mod.read_records(st.trials_path)
    sens = read_json(st.dir / "sensitivity.json")
    plan = st.dir / "plan"
    if a.action == "advise":
        r = stages.recommend(recs, st.spec, sp, sens, cfg)
        print(f"{r['action']}" + (f" (hypervolume gain {r['hvGain']:.1%} over the last {stages.HV_WINDOW} trials)" if r["hvGain"] is not None else ""))
        for x in r["reasons"]:
            print(f"  - {x}")
        if r["action"] == "switch to gp":
            plan.mkdir(exist_ok=True)
            (plan / "C.yaml").write_text(yaml.safe_dump(stages.plan_refine(sp, sens, st.spec, cfg), sort_keys=False), encoding="utf-8")
            print(f"  wrote {plan / 'C.yaml'}")
        return 0
    if sens is None:
        print("run `sensitivity --name` first")
        return 1
    plan.mkdir(exist_ok=True)
    for blk in stages.plan_blocks(sp, sens, st.spec):
        (plan / f"{blk['stage']}.yaml").write_text(yaml.safe_dump(blk, sort_keys=False), encoding="utf-8")
        print(f"  {blk['name']}: {len(blk['active'])} parameters ({blk['note']})")
    print(f"files in {plan}; run them in order with `study new --config` (each fixes the earlier blocks at their selected trial)")
    return 0


def _pool(cfg: dict, workers: int | None) -> EvalPool:
    return EvalPool(functools.partial(objective.BacktestRunner, cfg), workers or cfg["compute"]["workers"])


def cmd_robust(a, cfg) -> int:
    _, sp = _space(cfg)
    st = study_mod.Study.open(cfg, sp, a.name)
    with RunLock(st.dir / "run.lock", cfg["compute"]["lockStaleHours"]):
        pool = _pool(cfg, a.workers)
        try:
            st.verify_inputs(pool)
            doc = robust_mod.run(st, pool, cfg, a.top, out=sys.stderr)
        finally:
            pool.close()
    show = [{"trial": s["trial"], "id": s["trialId"], "cagr%": round(100 * s["f1"], 2), "depth%": round(100 * s["depth"], 2), "q25 cagr%": round(100 * s["q25F1"], 2) if s["q25F1"] is not None else "-",
             "within": f"{s['share']:.0%}", "cliffs": len(s["cliffs"]), "robust": "yes" if s["robust"] else "no", "pick": "<--" if s["trialId"] == doc["selected"] else ""} for s in doc["scores"]]
    print(_table(show, ["trial", "id", "cagr%", "depth%", "q25 cagr%", "within", "cliffs", "robust", "pick"]))
    print(f"\n{doc['decision']}" + (f": {doc['selected']} (run `gate --candidate {doc['selected']}`)" if doc["selected"] else ": no finalist is robust, the live configuration stays"))
    return 0


def cmd_gate(a, cfg) -> int:
    _, sp = _space(cfg)
    name, rec = study_mod.find_trial(cfg, a.candidate)
    st = study_mod.Study.open(cfg, sp, name)
    folder = Path(cfg["paths"]["data"]) / "candidates" / a.candidate
    with RunLock(st.dir / "run.lock", cfg["compute"]["lockStaleHours"]):
        pool = _pool(cfg, a.workers)
        try:
            st.verify_inputs(pool)
            ev = gate_mod.collect(st, pool, cfg, rec, st.ledger, out=sys.stderr)
        finally:
            pool.close()
    verdict, aud = gate_mod.checks(ev, cfg), None
    aud = gate_mod.audit(ev, cfg)
    gate_mod.save(folder, ev, verdict, aud, cfg)
    from hpo.viz import candidate
    candidate.candidate_report(folder, cfg)
    for k, v in verdict["checks"].items():
        print(f"  {'pass' if v['passed'] else 'FAIL'}  {k}" + ("" if v.get("blocking", True) else "  (label only)"))
    print(f"exploit audit: {'clean' if aud['passed'] else 'flagged ' + ', '.join(aud['flagged'])}; not assessed: {len(aud['notAssessed'])} items (see the report)")
    if verdict["label"]:
        print(f"label: {verdict['label']}")
    print(f"gate {'passed' if verdict['passed'] else 'NOT met: ' + ', '.join(verdict['failed'])}; report {folder / 'report.html'}")
    return 0 if verdict["passed"] else 3


def cmd_holdout(a, cfg) -> int:
    _, sp = _space(cfg)
    name, rec = study_mod.find_trial(cfg, a.candidate)
    st = study_mod.Study.open(cfg, sp, name)
    pool = _pool(cfg, 1)
    try:
        st.verify_inputs(pool)
        res = holdout_mod.score(st, pool, cfg, a.candidate, rec)
    finally:
        pool.close()
    print(f"holdout {res['window'][0]} to {res['window'][1]} (scored once): {'PASSED' if res['passed'] else 'NOT passed'}")
    for k, v in res["checks"].items():
        print(f"  {'pass' if v['passed'] else 'FAIL'}  {k}: {v['value']} against {v['limit']}")
    return 0 if res["passed"] else 3


def cmd_promote(a, cfg) -> int:
    _, sp = _space(cfg)
    name, rec = study_mod.find_trial(cfg, a.candidate)
    st = study_mod.Study.open(cfg, sp, name)
    folder = Path(cfg["paths"]["data"]) / "candidates" / a.candidate
    promote_mod.build(st, folder, cfg, cfg["paths"]["register"])
    print(f"promotable: wrote overlay.json, rollback.json, diff.md, dossier.md/json in {folder}")
    print("apply overlay.json to app/config by hand, bump config_version in your changelog, then run the 13-week shadow period: `shadow check --candidate " + a.candidate + "`")
    return 0


def cmd_shadow(a, cfg) -> int:
    import pandas as pd
    _, sp = _space(cfg)
    name, rec = study_mod.find_trial(cfg, a.candidate)
    st = study_mod.Study.open(cfg, sp, name)
    folder = Path(cfg["paths"]["data"]) / "candidates" / a.candidate
    dossier = read_json(folder / "dossier.json")
    if dossier is None:
        raise Refusal(f"{a.candidate} was not promoted (no dossier): the shadow limits are fixed by `promote`")
    nav = pd.read_csv(a.nav or "app/data/risk/nav/nav_shadow.csv", dtype={"date": str})
    realised = pd.to_numeric(nav.set_index("date").twr_index)
    pool = _pool(cfg, 1)
    try:
        st.verify_inputs(pool)
        rep = pool.submit({"replay": True, "values": st.space.complete(rec["values"]), "start": a.since or realised.index[0], "end": None}).result()
    finally:
        pool.close()
    table = shadow_mod.tracking(realised, rep["twr"])
    sh = dossier["shadow"]
    v = shadow_mod.verdict(table, sh["trackingGapPp"], sh["drawdownRollbackDepth"], sh["weeks"])
    write_json(folder / "shadow.json", {**v, "asOf": str(realised.index[-1]), "bandPp": sh["trackingGapPp"], "table": table.astype({"date": str}).to_dict("records")})
    print(f"shadow {v['status']}: week {v['weeks']} of {sh['weeks']}" + (f", gap {v['gapPp']:+.2f} pp (worst {v['worstGapPp']:.2f}), depth {v['depth']:.1%} (line {v['p95Depth']:.1%})" if v["weeks"] else ""))
    for r in v.get("reasons", []):
        print(f"  ROLL BACK: {r}")
    return 3 if v["status"] == "rollback" else 0


def cmd_report(a, cfg) -> int:
    from hpo.viz import report
    if getattr(a, "candidate", None):
        from hpo.viz import candidate
        out = candidate.candidate_report(Path(cfg["paths"]["data"]) / "candidates" / a.candidate, cfg)
        print(out)
        if a.open:
            webbrowser.open(out.resolve().as_uri())
        return 0
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
            try:
                out = report.study_report(folder, cfg, live=True)
            except OSError as e:  # a page that cannot be rewritten this time is retried on the next refresh, not a reason to stop the dashboard
                print(f"live page not updated ({e}); retrying", file=sys.stderr)
                time.sleep(cfg["compute"]["liveRefreshSeconds"])
                continue
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
    fr = sub.add_parser("front")
    fr.add_argument("--name", required=True)
    fr.add_argument("--select", choices=["calmar", "knee"], default="calmar")
    rb = sub.add_parser("robust")
    rb.add_argument("--name", required=True)
    rb.add_argument("--top", type=int)
    rb.add_argument("--workers", type=int)
    gt = sub.add_parser("gate")
    gt.add_argument("--candidate", required=True)
    gt.add_argument("--workers", type=int)
    for name in ("holdout", "promote"):
        sub.add_parser(name).add_argument("--candidate", required=True)
    sh = sub.add_parser("shadow")
    sh.add_argument("action", choices=["check"])
    sh.add_argument("--candidate", required=True)
    sh.add_argument("--nav")
    sh.add_argument("--since")
    sg = sub.add_parser("stages")
    sg.add_argument("action", choices=["plan", "advise"])
    sg.add_argument("--name", required=True)
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
        handler = {"space": cmd_space, "study": cmd_study, "sensitivity": cmd_sensitivity, "front": cmd_front, "stages": cmd_stages, "robust": cmd_robust, "gate": cmd_gate, "holdout": cmd_holdout, "promote": cmd_promote, "shadow": cmd_shadow, "report": cmd_report, "live": cmd_live, "ledger": cmd_ledger}.get(a.cmd)
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
