"""The study report and the live dashboard build from a fixture study (mid-run, finished), contain every chart, work offline and degrade on tiny studies."""

import io
import re
import tempfile
import unittest
from pathlib import Path

from hpo import sensitivity
from hpo.study import Study, read_records
from hpo.viz import report
from tests.hpo.fakes import FakeRunner, make_settings
from tests.hpo.test_space import load_space

ACTIVE = ["risk.stops.atrMultiplier", "risk.sizing.riskPerPositionPct", "risk.sizing.minNewOrderInr", "risk.sizing.nameCapPct.MidCap", "risk.sizing.noTradeBand.relative",
          "analyst.strategies.BULL.top_n", "analyst.strategies.BULL.top_n.off.MidCap"]


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        *_, cls.sp = load_space()

    def build(self, trials, **kw):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.cfg = make_settings(str(Path(tmp.name) / "data"))
        st = Study.create(self.cfg, self.sp, {"name": "r", "stage": "A", "sampler": "sobol", "active": ACTIVE, "trials": trials, "seed": 2, **kw})
        st.run(FakeRunner, workers=1, out=io.StringIO())
        return st

    def test_a_finished_study_report_has_every_chart_and_works_offline(self):
        st = self.build(60)
        import json
        res = sensitivity.analyse(self.sp, st.spec, read_records(st.trials_path), self.cfg)
        (st.dir / "sensitivity.json").write_text(json.dumps(res))
        out = report.study_report(st.dir, self.cfg)
        text = out.read_text(encoding="utf-8")
        for i in range(1, 11):
            self.assertIn(f'id="c{i}"', text)
        for i in (2, 3, 4, 5, 6, 8, 9):  # chart 7 needs three front trials; the fake landscape has a one-point front
            self.assertIn(f'id="plot-c{i}"', text, f"chart {i} should be a picture with 60 trials")
        own = text.split("</main>")[0] + text.split("</main>")[1].split("</script>", 1)[1]  # the page without the bundled Plotly library
        self.assertIsNone(re.search(r'(?:src|href)="https?://|@import|url\(http', own))  # no network at view time
        self.assertIn("Plotly.react", text)
        self.assertIn("prefers-color-scheme: dark", text)
        self.assertIn("effective N", text)
        self.assertNotIn("holdout", text.lower().replace("pre-holdout data only", ""))
        self.assertNotIn(str(self.cfg["paths"]["data"]), text)  # no path outside the report

    def test_a_mid_run_study_and_a_tiny_one_degrade_to_tables(self):
        st = self.build(3)
        text = report.study_report(st.dir, self.cfg).read_text(encoding="utf-8")
        for i in range(1, 11):
            self.assertIn(f'id="c{i}"', text)
        self.assertIn("Fewer than 5 scored trials", text)
        self.assertNotIn('id="plot-c2"', text)

    def test_the_live_page_refreshes_and_shows_the_live_charts_only(self):
        st = self.build(30)
        text = report.study_report(st.dir, self.cfg, live=True).read_text(encoding="utf-8")
        self.assertIn('http-equiv="refresh" content="10"', text)
        self.assertEqual(sorted(set(re.findall(r'<section class="chart" id="(c\d+)"', text))), sorted(["c1", "c2", "c3", "c4", "c9", "c10"]))

    def test_unfrozen_classes_are_a_warning_on_every_report(self):
        st = self.build(10, active=ACTIVE + ["risk.sizing.cashBufferPct"], allow_unfreeze=["risk.sizing.cashBufferPct"])
        self.assertIn("Frozen-class parameters", report.study_report(st.dir, self.cfg).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
