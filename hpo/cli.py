"""python -m hpo.cli <command>. Run from the repo root. Exit codes: 0 ok, 1 failed, 2 busy (run lock), 3 refused (cap, changed inputs, gate, holdout).

  --check                         validate hpo.json, the schema and the imports (no network, no writes)
  space build|check|show [--stage N]
  study new --config F | diagnose --name N (why trials are infeasible) | run --name N [--trials T] [--workers W] [--override-cap "reason"] | resume --name N | status --name N
  sensitivity --name N            importance and the freeze list (writes sensitivity.json and proposed_active.yaml in the study folder)
  report --name N | --candidate ID [--open]   HTML study report or candidate report;  live --name N  auto-refreshing dashboard while a study runs
  ledger show
  capital [--price P]             the smallest account at which the live sizing minimums can place an order, per bucket and regime
  probe [--set NAME=VALUE ...]    run ONE configuration (live default plus the --set values) with no abort rules and show it against every constraint limit
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
    bt, risk, analyst = api.base_configs(settings.bt_overrides(cfg))
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
    if a.action == "diagnose":
        recs = study_mod.read_records(Path(cfg["paths"]["data"]) / "studies" / a.name / "trials.jsonl")
        d = study_mod.diagnose(recs, cfg)
        e = d["ended"]
        print(f"{d['trials']} trials: {e['ok']} scored, {e['aborted']} aborted {d['abortReasons'] or ''}, {e['invalid']} invalid, {e['fail']} failed; {d['feasible']} feasible")
        print(_table([{"constraint": r["constraint"], "broken by": f"{r['violated']} of {d['scored']}", "limit": r["limit"], "best reached": r["best"] if r["best"] is not None else "-", "meaning": r["what"]} for r in d["constraints"]],
                     ["constraint", "broken by", "limit", "best reached", "meaning"]))
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
    res = st.run(factory, trials=getattr(a, "trials", None), workers=getattr(a, "workers", None), override_cap=getattr(a, "override_cap", None), bt_cfg=bt, accept_code_change=getattr(a, "accept_code_change", None))
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


def _value(text: str):
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    return int(text) if text.lstrip("-").isdigit() else float(text)


def probe_lines(out: dict, cfg: dict, changed: dict) -> list[str]:
    """One run against every limit: what this configuration actually reaches, so the constraint limits can be judged against real numbers."""
    c = cfg["constraints"]
    if out["status"] != "ok":
        return [f"status {out['status']}: {out.get('error') or out.get('attrs')}"]
    m, k = out["metrics"], out["constraints"]
    f1, depth = out["values"]
    fold = list(zip(m.get("foldCagr", []), m.get("foldDrawdown", []), m.get("foldFillsPerYear", [])))
    lim = lambda name, got, limit, bad: f"  {'BREAKS' if bad else 'ok    '} {name:<22} {got:<14} limit {limit}"  # noqa: E731
    lines = [f"changed from the live default: {changed or 'nothing'}", f"CAGR (CVaR of worst folds) {f1:.2%}   full-span CAGR {m.get('cagr') or 0:.2%}   max drawdown depth {depth:.2%}   calmar {m.get('calmar')}",
             "constraints (a run is feasible only when none breaks):",
             lim("fills", m.get("fills"), f">= {c['minFills']}", k["min_fills"] > 0), lim("fills per fold-year (worst)", f"{min(m.get('foldFillsPerYear') or [0]):.1f}", f">= {c['minFillsPerFoldYear']}", k["fills_per_fold_year"] > 0),
             lim("average exposure", f"{m.get('avgExposure', 0):.1%}", f">= {c['minAvgExposure']:.0%}", k["min_exposure"] > 0), lim("max drawdown depth", f"{depth:.1%}", f"<= {-c['maxDrawdown']:.0%}", k["dd_cap"] > 0),
             "folds (CAGR, max drawdown, fills per year):"]
    lines += [f"  fold {i + 1}: {a:>8.2%} {b:>8.2%} {n:>6.1f}" for i, (a, b, n) in enumerate(fold)]
    lines.append("regime shares: " + ", ".join(f"{r} {v:.0%}" for r, v in out.get("regimeShare", {}).items()))
    lines += _detail_lines(out.get("detail"))
    return lines


def _detail_lines(d: dict | None) -> list[str]:
    """Where a run's drawdown and cost came from: the deepest drawdown by date, the named stress windows, regimes, ladder rungs, and each year's activity."""
    if not d:
        return []
    nav = d["nav"]
    out = ["", "where it came from:"]
    dd = nav / nav.cummax() - 1.0
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    out.append(f"  deepest drawdown {-dd.min():.1%}: peak {peak} -> trough {trough}")
    out.append("  stress windows (return, max drawdown):")
    out += [f"    {n:<32} {w['return']:>8.1%} {w['maxDrawdown']:>8.1%}" for n, w in d["stressWindows"].items()] or ["    (none overlap the run)"]
    out.append("  regimes (share of days, annualised return, max drawdown):")
    out += [f"    {r:<8} {v['share']:>5.0%} {v['cagr'] if v['cagr'] is not None else float('nan'):>8.1%} {v['maxDrawdown'] if v['maxDrawdown'] is not None else float('nan'):>8.1%}" for r, v in d["regimes"].items()]
    rung = d["rung"].round().astype(int).value_counts(normalize=True).sort_index()
    out.append("  days on each ladder rung (0 = fully allowed, higher = cut back): " + ", ".join(f"{int(k)}: {v:.0%}" for k, v in rung.items()))
    pr, expo = d["profile"], d["exposure"].groupby(d["exposure"].index.str[:4]).mean()
    out.append("  by year: fills, turnover (x NAV), charges (% NAV), average exposure")
    out += [f"    {y}  {pr['fillsPerYear'].get(y, 0):>5}  {pr['turnover'].get(y, 0):>6.1f}  {100 * pr['costDrag'].get(y, 0):>6.2f}%  {expo.get(y, 0):>6.1%}" for y in sorted(set(pr["fillsPerYear"]) | set(expo.index))]
    return out


def cmd_probe(a, cfg) -> int:
    _, sp = _space(cfg)
    sets = dict(x.split("=", 1) for x in (a.set or []))
    changed = {n: _value(v) for n, v in sets.items()}
    unknown = [n for n in changed if n not in sp.dims]
    if unknown:
        raise Failed(f"unknown parameter(s) {unknown}: see `space show`")
    values = sp.complete(changed)
    pool = _pool(cfg, 1)
    try:
        t0 = time.time()
        out = pool.submit({"values": values, "noAbort": True, "detail": True}).result()
    finally:
        pool.close()
    print("\n".join(probe_lines(out, cfg, changed)))
    print(f"({time.time() - t0:.0f} s; no abort rules, nothing is recorded in any study)")
    return 0


def cmd_capital(a, cfg) -> int:
    from hpo import capital
    from backtest import api
    bt, risk, analyst = api.base_configs(settings.bt_overrides(cfg))
    r = capital.required(risk, analyst, a.price)
    print(f"live sizing: minimum new order Rs {r['minimumOrder']:,}; share price slack Rs {a.price:,.0f}; capital now: analyst Rs {analyst['capital']['floatingCapitalInr']:,}, backtest Rs {bt['capital']['inr']:,.0f}")
    print(_table([{"regime": x["regime"], "bucket": x["bucket"], "top_n": x["names"], "entry as % of NAV": round(100 * x["fraction"], 2), "limited by": x["limitedBy"], "capital needed": f"{x['needed']:,.0f}"} for x in r["rows"]],
                 ["regime", "bucket", "top_n", "entry as % of NAV", "limited by", "capital needed"]))
    print(f"\nbest bucket per regime: " + ", ".join(f"{g} Rs {b['needed']:,.0f} ({b['bucket']})" for g, b in r["bestPerRegime"].items() if b))
    print(f"simple name-cap check only (minimum order / largest name cap): Rs {r['namecapOnly']:,.0f} (too low: whole shares and bucket budgets are ignored)")
    print(f"an entry in at least one regime: Rs {r['anyRegime']:,.0f};  in EVERY regime: Rs {r['everyRegime']:,.0f}  (rounded up to Rs {capital.round_up(r['everyRegime']):,})")
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
    r.add_argument("--accept-code-change", dest="accept_code_change")
    ssub.add_parser("resume").add_argument("--name", required=True)
    ssub.add_parser("status").add_argument("--name", required=True)
    ssub.add_parser("diagnose").add_argument("--name", required=True)
    sub.add_parser("sensitivity").add_argument("--name", required=True)
    cp = sub.add_parser("capital")
    cp.add_argument("--price", type=float, default=2000.0, help="a typical share price, the rounding slack of whole shares (default 2000)")
    pr = sub.add_parser("probe")
    pr.add_argument("--set", action="append", metavar="NAME=VALUE")
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
        handler = {"space": cmd_space, "study": cmd_study, "sensitivity": cmd_sensitivity, "front": cmd_front, "capital": cmd_capital, "probe": cmd_probe, "stages": cmd_stages, "robust": cmd_robust, "gate": cmd_gate, "holdout": cmd_holdout, "promote": cmd_promote, "shadow": cmd_shadow, "report": cmd_report, "live": cmd_live, "ledger": cmd_ledger}.get(a.cmd)
        if handler is None:
            parser().print_help()
            return 1
        if a.cmd == "study" and a.action == "resume":
            a.trials = a.workers = a.override_cap = a.accept_code_change = None
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
