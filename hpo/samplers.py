"""The Optuna adapter: the only module that imports optuna. Pinned optuna==5.0.*; the surface used is create/load study, ask, tell,
enqueue_trial, set_constraint and the journal storage, so a sampler (or Optuna itself) can be swapped here.
"""

import warnings
from pathlib import Path

import optuna
from optuna.distributions import FloatDistribution, IntDistribution
from optuna.storages import JournalStorage
from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock

optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings("ignore", category=optuna.exceptions.ExperimentalWarning)  # QMCSampler is experimental; pinned, so no surprise
DIRECTIONS = ["maximize", "minimize"]  # f1 fold-CVaR CAGR, f2 drawdown depth


def distribution(spec: dict):
    """bool -> Int 0..1 (so a QMC sampler covers it), int -> IntDistribution, float -> FloatDistribution; log dims are sampled
    continuously and snapped to the register grid by Dim.repair."""
    if spec["kind"] == "bool":
        return IntDistribution(0, 1)
    if spec["kind"] == "int":
        return IntDistribution(int(spec["low"]), int(spec["high"]), log=spec["log"], step=1 if spec["log"] else int(spec["step"] or 1))
    return FloatDistribution(spec["low"], spec["high"], log=spec["log"], step=None if spec["log"] else spec["step"])


def distributions(space, names: list[str]) -> dict:
    return {n: distribution(space.dims[n].spec()) for n in names}


def make_sampler(kind: str, seed: int):
    if kind == "sobol":
        return optuna.samplers.QMCSampler(qmc_type="sobol", scramble=True, seed=seed, warn_independent_sampling=False, warn_asynchronous_seeding=False)
    if kind == "tpe":
        return optuna.samplers.TPESampler(multivariate=True, group=True, constant_liar=True, seed=seed, n_startup_trials=20)
    if kind == "gp":
        try:
            return optuna.samplers.GPSampler(seed=seed, n_startup_trials=20, deterministic_objective=True)
        except ModuleNotFoundError as e:  # Optuna's GP sampler needs PyTorch, which is an optional extra here
            raise ValueError("the gp sampler needs PyTorch: install with `uv sync --project hpo --extra gp` (CPU build: see doc/HPO_Implementation_Notes.md)") from e
    raise ValueError(f"unknown sampler {kind!r}: sobol, tpe or gp")


def storage(journal: Path):
    """The journal with Optuna's open-file lock: the default lock makes a symbolic link, which Windows refuses without an elevated privilege (WinError 1314)."""
    return JournalStorage(JournalFileBackend(str(journal), lock_obj=JournalFileOpenLock(str(journal))))


def open_study(name: str, journal: Path, sampler, create: bool):
    if create:
        return optuna.create_study(study_name=name, storage=storage(journal), sampler=sampler, directions=DIRECTIONS)
    return optuna.load_study(study_name=name, storage=storage(journal), sampler=sampler)


def trial_for(study, trial_id: int):
    """A handle on a trial already in the journal (a RUNNING one found at resume)."""
    return optuna.trial.Trial(study, trial_id)


COMPLETE, FAIL, RUNNING, WAITING = (optuna.trial.TrialState.COMPLETE, optuna.trial.TrialState.FAIL, optuna.trial.TrialState.RUNNING,
                                    optuna.trial.TrialState.WAITING)


def memory_study(space, names: list[str], rows: list[dict], directions: list[str]):
    """An in-memory study of finished trials (params, values), for the importance evaluators; the journal is never touched."""
    from optuna.trial import create_trial
    dists = distributions(space, names)
    study = optuna.create_study(directions=directions, sampler=optuna.samplers.RandomSampler(seed=0))
    study.add_trials([create_trial(params={n: (int(r["params"][n]) if space.dims[n].kind == "bool" else r["params"][n]) for n in names}, distributions=dists, values=r["values"])
                      for r in rows])
    return study


def ped_anova(study, names: list[str], target=None) -> dict[str, float]:
    """PED-ANOVA importances (sum to 1) of `names` for one objective of the study."""
    from optuna.importance import PedAnovaImportanceEvaluator
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = dict(PedAnovaImportanceEvaluator().evaluate(study, params=names, target=target))
    total = sum(raw.values())
    return {n: (v / total if total > 0 else 0.0) for n, v in raw.items()}
