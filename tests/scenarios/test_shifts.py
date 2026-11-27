from datetime import timedelta

from tests.common import MonitorTest
from drift_monitor.analysis import ks_distance
from drift_monitor.contracts import stamp
from drift_monitor.engine import tick
from drift_monitor.views import evidence
from scenarios import hour


class Scenarios(MonitorTest):
    def test_missing_blocks_even_large_shift(self):
        self.add(-24)
        self.add(0, scenario="missing", strength=3)
        checks = self.checks(self.run_at(1, 2))
        self.assertEqual(checks["missing.feature.spread"]["status"], "data_quality")
        self.assertEqual(checks["ks.feature.momentum"]["status"], "blocked_by_quality")
        self.assertEqual(checks["performance"]["status"], "blocked_by_quality")

    def test_seasonality(self):
        for n in (-24, -18, 6):
            self.add(n)
        morning, midday = [[r["features"]["momentum"] for r in hour(
            self.t0 + timedelta(hours=n), 50) if r["kind"] == "observation"] for n in (0, 6)]
        self.assertGreater(ks_distance(morning, midday), 0.8)
        checks = self.checks(self.run_at(7, 8))
        self.assertEqual(checks["ks.feature.momentum"]["value"], 0)
        self.assertEqual(checks["ks.feature.momentum"]["status"], "ok")

    def test_small_sample_and_empty_window(self):
        self.add(-24)
        self.add(0, count=10)
        for end in (1, 2):
            self.assertEqual(self.checks(self.run_at(end, end))["sample_size"]["status"], "inconclusive")
        self.assertEqual(self.db.execute("SELECT count(*) FROM tickets").fetchone()[0], 0)

    def test_abrupt_shift_storm_and_cooldown_expiry(self):
        for n in range(5):
            self.add(n - 24)
            self.add(n, strength=2)
        results = [self.run_at(n, n) for n in range(1, 6)]
        self.assertTrue(all(self.checks(r)["ks.feature.momentum"]["status"] == "distribution_shift"
                            for r in results))
        decisions = [d for r in results for d in r["decisions"] if d["signal"] == "ks.feature.momentum"]
        self.assertEqual([d["disposition"] for d in decisions], ["opened","cooldown","cooldown","opened","cooldown"])
        self.assertEqual(len({d["ticket_id"] for d in decisions}), 2)
        self.assertTrue(all(r["model_version"] == self.cfg["model_version"] for r in results))

    def test_gradual_shift(self):
        for n, shift in enumerate([0.0, 0.2, 0.4, 0.8, 1.6]):
            self.add(n - 24)
            self.add(n, strength=shift)
        values = [self.checks(self.run_at(n, n))["ks.feature.momentum"] for n in range(1, 6)]
        self.assertEqual(values[0]["status"], "ok")
        self.assertEqual(values[-1]["status"], "distribution_shift")
        self.assertEqual(sorted(v["value"] for v in values), [v["value"] for v in values])

    def test_delayed_labels_recover_via_worker(self):
        self.add(-24)
        self.add(0, scenario="delayed")
        first = self.run_at(1, 1)
        self.assertEqual(self.checks(first)["performance"]["status"], "pending_labels")
        self.assertEqual(self.checks(self.run_at(1, 2))["label_coverage"]["status"], "data_quality")
        ids = tick(self.db, self.name, stamp(self.t0 + timedelta(hours=4)))
        refreshed = evidence(self.db, ids[0])
        self.assertEqual(self.checks(refreshed)["label_coverage"]["current"], 1)
        self.assertEqual(self.checks(refreshed)["accuracy"]["status"], "ok")
        self.assertEqual(first, self.run_at(1, 1))

    def test_degradation_and_prediction_shift(self):
        self.add(-24, count=200)
        self.add(-23, count=200)
        self.add(0, scenario="degradation", count=200)
        self.add(1, scenario="prediction", count=200)
        checks = self.checks(self.run_at(1, 2))
        self.assertEqual(checks["ks.prediction"]["status"], "ok")
        for name in ("accuracy","calibration","brier"):
            self.assertEqual(checks[name]["status"], "performance_degradation")
        self.assertEqual(self.checks(self.run_at(2, 3))["ks.prediction"]["status"], "distribution_shift")
