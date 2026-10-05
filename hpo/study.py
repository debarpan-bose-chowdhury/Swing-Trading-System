"""A study: ask-and-tell over an EvalPool, every finished trial durable at once, stopped studies resume exactly.

Folder `hpo/data/studies/<name>/`:
  study.json       the resolved study file (active dimensions, sampler, budget, seed); never edited after `study new`
  journal.log      Optuna JournalStorage (the sampler's state)
  trials.jsonl     append-only, one record per trial: the audit source (also mirrored to backtest's Registry as registry.jsonl)
  returns/<id>.parquet   post-tax daily returns per simulated trial
  checkpoint.json  study hash, seed, identity (data, code, configs, span, folds), counters
  status.json  run.lock  report/

Order of a finished trial: trials.jsonl + returns, then Optuna tell, then checkpoint and status. A kill between two of them is repaired on
resume: a trial the journal still has RUNNING is told from its record (when written) or run again with its stored parameters (determinism
makes the re-run identical).
"""

import hashlib
import json
import random
import sys
import time
from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path

import pandas as pd
import yaml

from backtest import api
from hpo import ledger as ledger_mod
from hpo import objective, pareto, samplers
from hpo import space as space_mod
from hpo.errors import Failed, Refusal
from hpo.evalpool import EvalPool
from hpo.progress import Progress
from hpo.status import RunLock, read_json, write_json

IDENTITY_KEYS = ("dataHash", "codeSha", "baseConfig", "span", "folds", "writeOff", "schemaVersion")
RAW_KEYS = {"name", "stage", "sampler", "active", "exclude", "allow_unfreeze", "trials", "seed", "enqueueDefault", "variants", "fixed", "from"}


def resolve_spec(raw: dict, space, settings: dict) -> dict:
    """The study file with its tokens expanded to dimension names and every default filled in."""
    unknown = set(raw) - RAW_KEYS
    if unknown or not raw.get("name") or not raw.get("active"):
        raise Failed(f"study file: needs name and active; unknown keys {sorted(unknown)}")
    names = space.select(raw["active"], raw.get("exclude", []), raw.get("allow_unfreeze", []))
    if not names:
        raise Failed("study file: no active parameter is left after freezing (frozen classes need allow_unfreeze)")
    fixed = dict(raw.get("fixed") or {})
    bad = [n for n in fixed if n not in space.dims or n in names]
    if bad:
        raise Failed(f"study file: fixed names must be dimensions that are not active: {bad}")
    return {"name": str(raw["name"]), "fixed": fixed, "from": raw.get("from"), "stage": str(raw.get("stage", "?")), "sampler": raw.get("sampler", "sobol"), "trials": int(raw.get("trials", 64)),
            "seed": int(raw.get("seed", settings["compute"]["seed"])), "enqueueDefault": bool(raw.get("enqueueDefault", True)),
            "variants": int(raw.get("variants", settings["space"]["variants"])), "active": names, "unfrozen": space.unfrozen(names), "file": raw}


def spec_hash(spec: dict) -> str:
    keep = {k: spec.get(k) for k in ("name", "sampler", "seed", "enqueueDefault", "variants", "active", "fixed")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line] if path.exists() else []


def inactive_dims(space, shares: dict) -> list[str]:
    """Regime-specific dimensions of a regime that never occurred in the run (excluded from sensitivity counts)."""
    dead = [r for r, s in shares.items() if s == 0.0]
    return [n for n in space.names if any(f".strategies.{r}." in n or n.endswith(f"regimeCap.{r}") for r in dead)]


class Study:
    def __init__(self, settings: dict, space, spec: dict):
        self.cfg, self.space, self.spec = settings, space, spec
        self.dir = Path(settings["paths"]["data"]) / "studies" / spec["name"]
        self.journal, self.trials_path = self.dir / "journal.log", self.dir / "trials.jsonl"
        self.checkpoint_path, self.status_path = self.dir / "checkpoint.json", self.dir / "status.json"
        self.registry = api.registry(self.dir)
        self.ledger = ledger_mod.Ledger(Path(settings["paths"]["data"]) / "ledger" / "research_ledger.json", settings["ledger"]["effectiveNCap"],
                                        settings["ledger"]["defaultEffectiveRatio"])

    # --- creation and opening ------------------------------------------------------------------------------------------------------
    @classmethod
    def create(cls, settings: dict, space, raw: dict) -> "Study":
        """`study new`: always a new directory, so a fresh run never overwrites an old one."""
        spec = resolve_spec(raw, space, settings)
        st = cls(settings, space, spec)
        if st.dir.exists():
            raise Failed(f"study {spec['name']} already exists ({st.dir}); `study run` continues it, or pick another name")
        st.dir.mkdir(parents=True)
        write_json(st.dir / "study.json", {**spec, "schemaVersion": space.version})
        write_json(st.checkpoint_path, {"specHash": spec_hash(spec), "seed": spec["seed"], "identity": None, "counters": {"done": 0}, "optuna": samplers.optuna.__version__})
        samplers.open_study(spec["name"], st.journal, samplers.make_sampler(spec["sampler"], spec["seed"]), create=True)
        return st

    @classmethod
    def open(cls, settings: dict, space, name: str) -> "Study":
        doc = read_json(Path(settings["paths"]["data"]) / "studies" / name / "study.json")
        if doc is None:
            raise Failed(f"no study named {name} (`study new --config ...` creates one)")
        if doc["schemaVersion"] != space.version or set(doc["active"]) - set(space.names):
            raise Refusal(f"study {name} was made with schema {doc['schemaVersion']}; the space is now {space.version}: start a new study")
        return cls(settings, space, {k: v for k, v in doc.items() if k != "schemaVersion"})

    def records_ok(self) -> list[dict]:
        """Simulated (not cached) scored trials: the rows of the return matrix."""
        return [r for r in read_records(self.trials_path) if r["status"] == "ok" and not r["cacheHit"]]

    def verify_inputs(self, pool) -> dict:
        """The pool's data, code, configs and windows against the study's checkpoint (Refusal when they changed): robustness and gate runs must see what the study saw."""
        ck, ident = read_json(self.checkpoint_path), pool.identity()
        self._check_inputs(ck, ident)
        return ident

    # --- the run -------------------------------------------------------------------------------------------------------------------
    def run(self, runner_factory, trials: int | None = None, workers: int | None = None, override_cap: str | None = None, out=None, bt_cfg: dict | None = None) -> dict:
        spec, cfg = self.spec, self.cfg
        target, workers = trials or spec["trials"], workers or cfg["compute"]["workers"]
        with RunLock(self.dir / "run.lock", cfg["compute"]["lockStaleHours"]):
            pool = EvalPool(runner_factory, workers)
            try:
                return self._run(pool, target, workers, override_cap, out or sys.stderr, bt_cfg)
            finally:
                pool.close()

    def _run(self, pool: EvalPool, target: int, workers: int, override_cap: str | None, out, bt_cfg: dict | None) -> dict:
        spec, cfg, space = self.spec, self.cfg, self.space
        ck = read_json(self.checkpoint_path)
        identity = pool.identity()
        self._check_inputs(ck, identity)
        self.identity, self.bt_cfg = identity, bt_cfg
        self.records = read_records(self.trials_path)
        self.cache = {r["pointKey"]: r for r in self.records if r["status"] in ("ok", "aborted", "invalid") and not r.get("cacheHit")}
        plan = self.ledger.check(spec["name"], target, override_cap)
        sampler = samplers.make_sampler(spec["sampler"], spec["seed"])
        study = samplers.open_study(spec["name"], self.journal, sampler, create=False)
        self._study, self._pool = study, pool
        dists = samplers.distributions(space, spec["active"])
        if not study.get_trials(deepcopy=False):
            self._enqueue(study, target)
        resume = self._reconcile(study)
        started_n = sum(ft.state != samplers.WAITING for ft in study.get_trials(deepcopy=False))
        started, done0 = time.time(), len(self.records)
        self.eff = {"n": self.ledger.effective_total(), "at": len(self.records)}
        prog, inflight = Progress(out), {}
        state = "finished"
        try:
            while True:
                while len(inflight) < workers and (resume or started_n < target):
                    if resume:
                        trial, params = resume.pop(0)
                    else:
                        trial, params = self._ask(study, dists)
                        started_n += 1
                    fut = self._dispatch(trial, params)
                    if fut is not None:
                        inflight[fut] = (trial, params)
                    st = self._stats(target, started, done0, plan)
                    prog.update(st)
                if not inflight:
                    break
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    trial, params = inflight.pop(fut)
                    try:
                        res = fut.result()
                    except BaseException as e:  # noqa: BLE001  worker crash or pool failure: a FAIL trial, recorded
                        res = {**objective.placeholder("fail"), "error": f"{type(e).__name__}: {e}", "seconds": 0.0}
                    self._finish(study, trial, params, res)
                    self._check_fail_share()
                    prog.update(self._stats(target, started, done0, plan))
        except KeyboardInterrupt:
            state = "stopped"
        except Failed:
            state = "failed"
            raise
        finally:
            self._effective(final=True)
            st = self._stats(target, started, done0, plan, state)
            write_json(self.status_path, st)
            prog.update(st, final=True)
        return st

    def _check_inputs(self, ck: dict, identity: dict) -> None:
        if ck["specHash"] != spec_hash(self.spec):
            raise Refusal("the study file changed since `study new`: start a new study")
        if ck["identity"] is None:
            ck["identity"] = {k: identity[k] for k in IDENTITY_KEYS}
        else:
            changed = [k for k in IDENTITY_KEYS if ck["identity"].get(k) != identity[k]]
            if changed:
                raise Refusal(f"inputs changed since the study started ({', '.join(changed)}): its trials are no longer comparable; start a new study")
        if ck.get("optuna") != samplers.optuna.__version__:
            print(f"warning: Optuna was {ck.get('optuna')} when this study started, now {samplers.optuna.__version__}", file=sys.stderr)
            ck["optuna"] = samplers.optuna.__version__
        write_json(self.checkpoint_path, ck)

    # --- trial 0 and the variants around it -----------------------------------------------------------------------------------------
    def _optuna_value(self, name: str, v):
        return int(v) if self.space.dims[name].kind == "bool" else v

    def _enqueue(self, study, target: int) -> None:
        spec, space = self.spec, self.space
        if not spec["enqueueDefault"] or target < 1:
            return
        base = {n: space.defaults[n] for n in spec["active"]}
        study.enqueue_trial({n: self._optuna_value(n, space.dims[n].repair(v)) for n, v in base.items()})
        rng = random.Random(spec["seed"])
        for _ in range(min(spec["variants"], target - 1)):
            p = dict(base)
            for n in rng.sample(spec["active"], min(len(spec["active"]), rng.randint(1, 3))):
                d = space.dims[n]
                p[n] = (not p[n]) if d.kind == "bool" else p[n] + (d.step or (d.high - d.low) / 20) * rng.choice((-1, 1))
            study.enqueue_trial({n: self._optuna_value(n, space.dims[n].repair(v)) for n, v in p.items()})

    def _ask(self, study, dists):
        trial = study.ask(fixed_distributions=dists)
        return trial, dict(trial.params)

    # --- resume ---------------------------------------------------------------------------------------------------------------------
    def _reconcile(self, study) -> list:
        """Journal against trials.jsonl; the trials still RUNNING in the journal are returned to be run again (or told, when already recorded)."""
        by_number = {r["trial"]: r for r in self.records}
        resume = []
        for ft in study.get_trials(deepcopy=False):
            rec = by_number.get(ft.number)
            if ft.state == samplers.WAITING:
                continue  # enqueued (trial 0 and its variants), not started yet
            if ft.state == samplers.RUNNING:
                trial = samplers.trial_for(study, ft._trial_id)
                if rec is not None:
                    self._tell(study, trial, rec)
                else:
                    resume.append((trial, dict(ft.params)))
            elif rec is None:
                raise Failed(f"journal trial {ft.number} has no record in trials.jsonl: the study files disagree")
            elif self.space.key(self._values(rec["params"])) != rec["pointKey"]:
                raise Failed(f"trial {ft.number}: the point in trials.jsonl does not match the journal")
        known = {ft.number for ft in study.get_trials(deepcopy=False)}
        missing = [n for n in by_number if n not in known]
        if missing:
            raise Failed(f"trials.jsonl has trials the journal does not ({missing[:5]}): the study files disagree")
        return resume

    # --- one trial ------------------------------------------------------------------------------------------------------------------
    def _values(self, params: dict) -> dict:
        return self.space.complete({**self.spec.get("fixed", {}), **{n: (bool(v) if self.space.dims[n].kind == "bool" else v) for n, v in params.items()}})

    def _dispatch(self, trial, params: dict):
        """Run the point: a rejected point and a cache hit finish at once (no simulation); anything else goes to the pool."""
        try:
            values = self._values(params)
        except space_mod.InvalidPoint as e:
            self._finish(self._study, trial, params, objective.placeholder("invalid", error=str(e)), values=None)
            return None
        key = self.space.key(values)
        try:
            risk, analyst, _ = self.space.decode(values)
            self._config_hash = api.config_hash(self.bt_cfg, risk, analyst) if self.bt_cfg else None
        except space_mod.InvalidPoint as e:
            self._finish(self._study, trial, params, objective.placeholder("invalid", error=str(e)), values=values)
            return None
        if key in self.cache:
            hit = self.cache[key]
            res = {"status": hit["status"], "values": hit["objectives"], "constraints": hit["constraints"], "metrics": hit["metrics"], "regimeShare": hit.get("regimeShare", {}),
                   "attrs": hit.get("attrs", {}), "returns": None, "seconds": 0.0, "cacheOf": hit["trialId"], "error": hit.get("error")}
            self._finish(self._study, trial, params, res, values=values)
            return None
        return self._pool.submit({"values": values})

    def _finish(self, study, trial, params: dict, res: dict, values: dict | None = None) -> None:
        values = values if values is not None else self._safe_values(params)
        key = json.dumps(values, sort_keys=True, separators=(",", ":")) if values else json.dumps(params, sort_keys=True)
        span = self.identity["span"]
        tid = api.trial_id(key, span[0], span[1], "tuning")
        cons = res["constraints"]
        ok = res["status"] == "ok"
        rec = {"trial": trial.number, "trialId": tid, "pointKey": key, "params": {k: (float(v) if not isinstance(v, bool) else int(v)) for k, v in params.items()}, "values": values,
               "status": res["status"], "objectives": res["values"], "constraints": cons, "feasible": ok and objective.feasible(cons), "metrics": res["metrics"],
               "regimeShare": res.get("regimeShare", {}), "inactive": inactive_dims(self.space, res.get("regimeShare", {})) if ok else [], "attrs": res.get("attrs", {}),
               "error": res.get("error"), "seconds": res.get("seconds", 0.0), "cacheHit": "cacheOf" in res, "cacheOf": res.get("cacheOf"), "stage": self.spec["stage"],
               "sampler": self.spec["sampler"], "seed": self.spec["seed"], "schemaVersion": self.space.version, "codeSha": self.identity["codeSha"],
               "dataHash": self.identity["dataHash"], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if ok and not rec["cacheHit"]:
            self.registry.append({"trialId": tid, "configHash": getattr(self, "_config_hash", None), "paramsKey": key, "params": values,
                                  "window": {"start": span[0], "end": span[1], "kind": "tuning", "fold": None},
                                  "metrics": {"postTaxCagr": res["metrics"].get("cagr"), "maxDrawdown": res["metrics"].get("maxDrawdown"), "ulcerIndex": res["metrics"].get("ulcerIndex"),
                                              "sharpe": res["metrics"].get("sharpe"), "fills": res["metrics"].get("fills")},
                                  "seed": self.spec["seed"], "codeSha": rec["codeSha"], "dataHash": rec["dataHash"], "at": rec["at"]}, res["returns"])
        with open(self.trials_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True, default=float) + "\n")
        self.records.append(rec)
        if res["status"] in ("ok", "aborted", "invalid") and not rec["cacheHit"]:
            self.cache[key] = rec
        self._tell(study, trial, rec)
        write_json(self.checkpoint_path, {**read_json(self.checkpoint_path), "counters": {"done": len(self.records)}})

    def _safe_values(self, params: dict):
        try:
            return self._values(params)
        except space_mod.InvalidPoint:
            return None

    def _tell(self, study, trial, rec: dict) -> None:
        if rec["status"] == "fail":
            study.tell(trial, state=samplers.FAIL)
            return
        for k, v in rec["constraints"].items():
            trial.set_constraint(k, float(v))
        trial.set_user_attr("status", rec["status"])
        trial.set_user_attr("feasible", rec["feasible"])
        study.tell(trial, [float(x) for x in rec["objectives"]])

    def _check_fail_share(self) -> None:
        c = self.cfg["compute"]
        window = self.records[-c["failWindow"]:]
        if len(window) >= c["failWindow"] and sum(r["status"] == "fail" for r in window) / len(window) > c["failShareAbort"]:
            raise Failed(f"more than {c['failShareAbort']:.0%} of the last {c['failWindow']} trials failed (see the error fields in {self.trials_path}); the study stops")

    # --- numbers for status, progress and the ledger -----------------------------------------------------------------------------
    def _effective(self, final: bool = False) -> None:
        n = len(self.records)
        if not final and n - self.eff["at"] < 100:
            return
        frame = {}
        for r in self.records:
            if r["status"] == "ok" and not r["cacheHit"] and r["trialId"] not in frame:
                frame[r["trialId"]] = self.registry.returns(r["trialId"])
        eff = ledger_mod.effective_n(pd.DataFrame(frame).dropna(), self.cfg["ledger"]["clusterRhoMax"]) if len(frame) > 1 else len(frame)
        raw = len({r["pointKey"] for r in self.records})
        self.ledger.record(self.spec["name"], self.spec["stage"], raw, eff)
        self.eff = {"n": self.ledger.effective_total(), "at": n}

    def _stats(self, target: int, started: float, done0: int, plan: dict, state: str = "running") -> dict:
        self._effective()
        recs = self.records
        feas = [r for r in recs if r["feasible"]]
        pts = [tuple(r["objectives"]) for r in feas]
        fi = pareto.front(pts)
        hv = pareto.hypervolume(pts, tuple(self.cfg["objectives"]["hvReference"]))
        elapsed = time.time() - started
        rate = (len(recs) - done0) / elapsed * 3600 if elapsed > 0 and len(recs) > done0 else 0.0
        left = max(0, target - len(recs))
        st = {"name": self.spec["name"], "stage": self.spec["stage"], "sampler": self.spec["sampler"], "state": state, "planned": target, "done": len(recs), "feasible": len(feas),
              "fail": sum(r["status"] == "fail" for r in recs), "aborted": sum(r["status"] == "aborted" for r in recs), "invalid": sum(r["status"] == "invalid" for r in recs),
              "bestCagr": max((pts[i][0] for i in fi), default=None), "bestDepth": min((pts[i][1] for i in fi), default=None), "hypervolume": hv, "trialsPerHour": rate,
              "elapsedS": elapsed, "etaS": left / rate * 3600 if rate else None, "effectiveN": self.eff["n"], "cap": self.cfg["ledger"]["effectiveNCap"],
              "frontSize": len(fi), "unfrozen": self.spec["unfrozen"], "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
        write_json(self.status_path, st)
        return st


def load_spec_file(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def find_trial(settings: dict, trial_id: str) -> tuple[str, dict]:
    """(study name, trials.jsonl record) of a trial id (the 12-character hash shown by `front` and `robust`) in any study."""
    root = Path(settings["paths"]["data"]) / "studies"
    for folder in sorted(root.glob("*")) if root.exists() else []:
        for r in read_records(folder / "trials.jsonl"):
            if r["trialId"] == trial_id and r["status"] == "ok" and not r["cacheHit"]:
                return folder.name, r
    raise Failed(f"no scored trial with id {trial_id} in any study under {root}")
