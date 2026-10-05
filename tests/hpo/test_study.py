"""Study mechanics on a fake runner: budget, trial 0, determinism, resume equality, cache, failures, locks, cap, changed inputs, spawn pool."""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hpo import samplers, space as space_mod
from hpo.errors import Busy, Failed, Refusal
from hpo.study import Study, read_records
from tests.hpo.fakes import FakeRunner, REPO, make_runner, make_settings
from tests.hpo.test_space import load_space

SPEC = {"name": "t1", "stage": "0", "sampler": "sobol", "active": ["stage:0"], "trials": 12, "seed": 3}


class StudyCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg0, cls.risk, cls.analyst, cls.sp = load_space()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cfg = make_settings(str(self.tmp / "data"))

    def new(self, **spec) -> Study:
        return Study.create(self.cfg, self.sp, {**SPEC, **spec})

    def run_study(self, st: Study, factory=FakeRunner, **kw) -> dict:
        return st.run(factory, workers=kw.pop("workers", 1), out=io.StringIO(), **kw)


class RunTests(StudyCase):
    def test_runs_to_the_budget_and_trial_zero_is_the_live_default(self):
        st = self.new()
        status = self.run_study(st)
        recs = read_records(st.trials_path)
        self.assertEqual((len(recs), status["done"], status["state"]), (12, 12, "finished"))
        self.assertEqual({k: recs[0]["values"][k] for k in st.spec["active"]}, {k: self.sp.defaults[k] for k in st.spec["active"]})
        self.assertEqual(recs[0]["values"], self.sp.complete(self.sp.defaults))
        self.assertTrue(all(r["status"] == "ok" and r["schemaVersion"] == "1.0.0" and r["codeSha"] == "code0" for r in recs))
        self.assertEqual(len(list((st.dir / "returns").glob("*.parquet"))), len({r["trialId"] for r in recs}))
        self.assertEqual(len(st.registry.records()), len({r["pointKey"] for r in recs}))
        self.assertTrue((st.dir / "status.json").exists() and not (st.dir / "run.lock").exists())
        self.assertGreater(status["hypervolume"], 0)
        self.assertEqual(status["feasible"], sum(r["feasible"] for r in recs))

    def test_study_new_never_overwrites_an_old_study(self):
        self.new()
        with self.assertRaisesRegex(Failed, "already exists"):
            self.new()

    def test_a_study_that_cannot_be_created_leaves_no_folder_behind(self):
        with patch.object(samplers, "open_study", side_effect=OSError("journal refused")), self.assertRaises(OSError):
            self.new()
        self.assertFalse((self.tmp / "data" / "studies" / "t1").exists())
        self.assertEqual(self.run_study(self.new())["state"], "finished")  # the same name can be created again straight away

    def test_same_seed_same_sequence(self):
        a, b = self.new(name="a"), self.new(name="b")
        self.run_study(a)
        self.run_study(b)
        self.assertEqual([r["pointKey"] for r in read_records(a.trials_path)], [r["pointKey"] for r in read_records(b.trials_path)])

    def test_resume_after_a_kill_equals_an_uninterrupted_run(self):
        whole, cut = self.new(name="whole"), self.new(name="cut")
        self.run_study(whole)
        self.assertEqual(self.run_study(cut, lambda: FakeRunner(boom_on_run=5))["state"], "stopped")  # killed inside trial 4 (counting from 0)
        self.assertEqual(len(read_records(cut.trials_path)), 4)
        study = samplers.open_study("cut", cut.journal, samplers.make_sampler("sobol", 3), create=False)
        self.assertEqual(sum(t.state == samplers.RUNNING for t in study.get_trials()), 1)
        self.run_study(Study.open(self.cfg, self.sp, "cut"))
        a, b = read_records(whole.trials_path), read_records(cut.trials_path)
        self.assertEqual([(r["trial"], r["pointKey"], r["objectives"]) for r in a], [(r["trial"], r["pointKey"], r["objectives"]) for r in b])
        self.assertEqual({t.number for t in samplers.open_study("cut", cut.journal, None, create=False).get_trials()}, {r["trial"] for r in b})
        self.assertEqual(len(b), len({r["trial"] for r in b}))  # no duplicate trials

    def test_a_trial_recorded_but_not_told_is_told_on_resume(self):
        st = self.new()
        real = Study._tell
        calls = {"n": 0}

        def flaky(self_, study, trial, rec):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt  # killed after trials.jsonl was written, before the journal heard
            return real(self_, study, trial, rec)

        with patch.object(Study, "_tell", flaky):
            self.assertEqual(self.run_study(st)["state"], "stopped")
        self.assertEqual(len(read_records(st.trials_path)), 3)
        self.run_study(Study.open(self.cfg, self.sp, "t1"))
        self.assertEqual(len(read_records(st.trials_path)), 12)
        self.assertEqual(sum(t.state == samplers.COMPLETE for t in samplers.open_study("t1", st.journal, None, create=False).get_trials()), 12)

    def test_more_trials_on_a_finished_study_continue_it(self):
        st = self.new()
        self.run_study(st)
        self.assertEqual(self.run_study(Study.open(self.cfg, self.sp, "t1"), trials=16)["done"], 16)

    def test_a_repeated_point_is_a_cache_hit_not_a_second_simulation(self):
        made = []

        def factory():
            made.append(FakeRunner())
            return made[-1]

        st = self.new(active=["risk.cooldown.reentryAboveStopClose"], trials=8, variants=0)
        self.run_study(st, factory)
        recs = read_records(st.trials_path)
        self.assertEqual(len(recs), 8)
        self.assertEqual(made[0].calls, 2)  # the dimension has two values
        hits = [r for r in recs if r["cacheHit"]]
        self.assertEqual(len(hits), 6)
        self.assertTrue(all(r["cacheOf"] for r in hits))

    def test_a_rejected_point_costs_no_simulation_and_is_infeasible(self):
        made = []

        def factory():
            made.append(FakeRunner())
            return made[-1]

        st = self.new(active=["risk.sizing.minNewOrderInr"], trials=10, variants=0)
        real = st.space.decode

        def decode(v):
            if v["risk.sizing.minNewOrderInr"] > 12000:
                raise space_mod.InvalidPoint("scripted")
            return real(v)

        with patch.object(st.space, "decode", decode):
            self.run_study(st, factory)
        recs = read_records(st.trials_path)
        bad = [r for r in recs if r["status"] == "invalid"]
        self.assertTrue(bad)
        self.assertTrue(all(not r["feasible"] and r["constraints"]["valid"] == 1.0 for r in bad))
        self.assertEqual(made[0].calls, len(recs) - len(bad) - sum(r["cacheHit"] for r in recs))
        study = samplers.open_study("t1", st.journal, None, create=False)
        self.assertEqual(sum(t.state == samplers.COMPLETE for t in study.get_trials()), 10)


class FailureTests(StudyCase):
    def test_an_exception_is_a_fail_trial_and_is_recorded(self):
        st = self.new(trials=6)
        self.run_study(st, lambda: FakeRunner(fail_if=lambda v: v["risk.sizing.nameCapPct.MidCap"] != 0.08))
        recs = read_records(st.trials_path)
        failed = [r for r in recs if r["status"] == "fail"]
        self.assertTrue(failed and all("scripted failure" in r["error"] for r in failed))
        study = samplers.open_study("t1", st.journal, None, create=False)
        self.assertEqual(sum(t.state == samplers.FAIL for t in study.get_trials()), len(failed))

    def test_more_than_five_percent_failing_in_a_window_aborts_the_study(self):
        self.cfg = make_settings(str(self.tmp / "data"), compute__failWindow=10)
        st = self.new(trials=40)
        with self.assertRaisesRegex(Failed, "failed"):
            self.run_study(st, lambda: FakeRunner(fail_if=lambda v: True))
        self.assertEqual(json.loads((st.dir / "status.json").read_text())["state"], "failed")


class GuardTests(StudyCase):
    def test_a_second_run_on_the_same_study_is_busy(self):
        st = self.new()
        (st.dir / "run.lock").write_text(json.dumps({"pid": os.getppid(), "time": time.time()}))
        with self.assertRaises(Busy):
            self.run_study(st)

    def test_a_stale_lock_is_taken_over(self):
        st = self.new()
        (st.dir / "run.lock").write_text(json.dumps({"pid": os.getppid(), "time": time.time() - 7 * 3600}))
        self.assertEqual(self.run_study(st)["state"], "finished")

    def test_the_effective_n_cap_refuses_a_study_unless_overridden_and_logs_the_override(self):
        led = self.new().ledger
        led.record("earlier", "A", 400, 199)
        with self.assertRaisesRegex(Refusal, "cap"):
            self.run_study(Study.open(self.cfg, self.sp, "t1"))
        st = Study.open(self.cfg, self.sp, "t1")
        self.run_study(st, override_cap="re-run after a data fix")
        self.assertEqual(st.ledger.doc["overrides"][0]["reason"], "re-run after a data fix")

    def test_changed_inputs_refuse_a_resume(self):
        st = self.new()
        self.run_study(st, trials=4)

        class Other(FakeRunner):
            def identity(self):
                return {**super().identity(), "dataHash": "data1"}

        with self.assertRaisesRegex(Refusal, "dataHash"):
            self.run_study(Study.open(self.cfg, self.sp, "t1"), Other)

    def test_a_new_commit_can_be_accepted_on_request_and_is_logged_but_changed_data_never_can(self):
        st = self.new()
        self.run_study(st, trials=4)

        class NewCommit(FakeRunner):
            def identity(self):
                return {**super().identity(), "codeSha": "code1"}

        class NewData(NewCommit):
            def identity(self):
                return {**super().identity(), "dataHash": "data1"}

        with self.assertRaisesRegex(Refusal, "accept-code-change"):
            self.run_study(Study.open(self.cfg, self.sp, "t1"), NewCommit)
        self.assertEqual(self.run_study(Study.open(self.cfg, self.sp, "t1"), NewCommit, trials=6, accept_code_change="pulled a tooling fix")["done"], 6)
        ck = json.loads((st.dir / "checkpoint.json").read_text())
        self.assertEqual((ck["codeChanges"][0]["from"], ck["codeChanges"][0]["to"], ck["codeChanges"][0]["reason"]), ("code0", "code1", "pulled a tooling fix"))
        self.assertEqual(self.run_study(Study.open(self.cfg, self.sp, "t1"), NewCommit, trials=8)["done"], 8)  # the new commit is now the study's own
        with self.assertRaisesRegex(Refusal, "dataHash"):
            self.run_study(Study.open(self.cfg, self.sp, "t1"), NewData, accept_code_change="x")

    def test_a_changed_study_file_cannot_resume(self):
        st = self.new()
        doc = json.loads((st.dir / "study.json").read_text())
        doc["active"] = doc["active"][:-1]
        (st.dir / "study.json").write_text(json.dumps(doc))
        with self.assertRaisesRegex(Refusal, "study file changed"):
            self.run_study(Study.open(self.cfg, self.sp, "t1"))

    def test_frozen_parameters_need_allow_unfreeze_and_are_listed(self):
        st = self.new(name="u", active=["risk.sizing.cashBufferPct", "risk.sizing.minNewOrderInr"], allow_unfreeze=["risk.sizing.cashBufferPct"])
        self.assertEqual(st.spec["unfrozen"], ["risk.sizing.cashBufferPct"])
        with self.assertRaisesRegex(Failed, "no active parameter"):
            self.new(name="v", active=["risk.sizing.cashBufferPct"])


class DiagnoseTests(StudyCase):
    def test_it_names_the_constraint_that_blocks_every_trial_and_the_best_value_reached(self):
        from hpo.study import diagnose
        st = self.new(trials=10)
        self.run_study(st)
        d = diagnose(read_records(st.trials_path), self.cfg)
        self.assertEqual((d["trials"], d["scored"], d["ended"]["ok"]), (10, 10, 10))
        by = {r["constraint"]: r for r in d["constraints"]}
        self.assertEqual(set(by), {"min_fills", "fills_per_fold_year", "min_exposure", "dd_cap"})
        self.assertEqual(by["min_fills"]["limit"], 300)
        self.assertLessEqual(by["min_fills"]["best"], 400)
        self.assertEqual(by["min_exposure"]["violated"], 0)  # the fake runner holds 70% exposure
        recs = [{"status": "ok", "feasible": False, "constraints": {"min_fills": 100, "fills_per_fold_year": 0, "min_exposure": 0.1, "dd_cap": -0.1}, "metrics": {"fills": 200, "avgExposure": 0.3, "maxDrawdown": -0.2,
                                                                                                                                                 "foldFillsPerYear": [20, 9]}, "attrs": {}},
                {"status": "aborted", "feasible": False, "constraints": {}, "metrics": {}, "attrs": {"abortReason": "no fills"}}]
        d2 = diagnose(recs, self.cfg)
        by2 = {r["constraint"]: r for r in d2["constraints"]}
        self.assertEqual((d2["abortReasons"], by2["min_fills"]["violated"], by2["min_fills"]["best"], by2["fills_per_fold_year"]["best"], by2["min_exposure"]["best"]), ({"no fills": 1}, 1, 200, 9, 0.3))


class WindowsTests(StudyCase):
    def test_the_journal_never_needs_a_symbolic_link(self):
        """Windows refuses os.symlink without an elevated privilege (WinError 1314); Optuna's default journal lock is a symlink, so the adapter uses the open-file lock."""
        with patch("os.symlink", side_effect=OSError(1314, "A required privilege is not held by the client")):
            st = self.new(trials=4)
            self.assertEqual(self.run_study(st)["done"], 4)
            self.assertEqual(self.run_study(Study.open(self.cfg, self.sp, "t1"), trials=6)["done"], 6)
        self.assertFalse(list(st.dir.glob("*.lock*")))  # no stale journal lock is left behind

    def test_the_schema_check_does_not_depend_on_line_endings(self):
        from hpo import space as space_mod
        for ending in (b"\n", b"\r\n"):
            reg, extra = self.tmp / "r.csv", self.tmp / "e.json"
            reg.write_bytes((REPO / "doc/parameter_register.csv").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", ending))
            extra.write_bytes((REPO / "hpo/config/space_extra.json").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", ending))
            doc = space_mod.build_schema(reg, extra, self.risk, self.analyst, self.cfg0["space"])
            self.assertEqual(doc, self.sp.schema)


class PoolTests(StudyCase):
    def test_a_spawn_pool_of_two_workers_finishes_and_leaves_no_lock(self):
        st = self.new(trials=8)
        status = self.run_study(st, make_runner, workers=2)
        self.assertEqual(status["done"], 8)
        recs = read_records(st.trials_path)
        self.assertEqual(sorted(r["trial"] for r in recs), list(range(8)))
        self.assertFalse((st.dir / "run.lock").exists())

    def test_nothing_is_written_outside_the_data_folder(self):
        def snapshot():
            return {str(p.relative_to(REPO)) for p in REPO.rglob("*") if p.is_file() and not any(x in p.parts for x in (".git", ".venv", "__pycache__", ".pytest_cache"))}

        before = snapshot()
        self.run_study(self.new(trials=4))
        self.assertEqual(snapshot() - before, set())


if __name__ == "__main__":
    unittest.main()


class FileReplaceTests(unittest.TestCase):
    """Windows refuses to replace a file that is open elsewhere (a browser showing live.html, OneDrive syncing): retry, then fall back to a plain write."""

    def test_a_transient_permission_error_is_retried(self):
        from hpo import status
        with tempfile.TemporaryDirectory() as t:
            target, real, calls = Path(t) / "s.json", os.replace, {"n": 0}

            def flaky(a, b):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(5, "Access is denied")
                return real(a, b)

            with patch("os.replace", flaky):
                status.write_json(target, {"a": 1})
            self.assertEqual((json.loads(target.read_text()), calls["n"]), ({"a": 1}, 3))
            self.assertEqual([p.name for p in Path(t).iterdir()], ["s.json"])

    def test_a_target_that_stays_locked_is_still_written_and_no_temp_file_is_left(self):
        from hpo import status
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "live.html"
            target.write_text("old")
            tmp = Path(t) / "live.tmp"
            tmp.write_text("new")
            with patch("os.replace", side_effect=PermissionError(5, "Access is denied")), patch("time.sleep"):
                status.replace(tmp, target, tries=3)
            self.assertEqual(target.read_text(), "new")
            self.assertFalse(tmp.exists())
