from tests.common import MonitorTest
from drift_monitor.analysis import ks_distance, performance
from drift_monitor.contracts import Invalid, instant, policy, record, window


class Core(MonitorTest):
    def test_exact_ks_and_ties(self):
        self.assertEqual(ks_distance([0, 0, 1], [0, 1, 1]), 1/3)
        self.assertEqual(ks_distance([2], [2, 2]), 0)
        self.assertEqual(ks_distance([0, 1], [2, 3]), 1)

    def test_binary_metrics_with_boundary_probabilities(self):
        scores = performance([(0, 0), (0.5, 1), (1, 0)])
        self.assertAlmostEqual(scores["accuracy"], 2/3)
        self.assertAlmostEqual(scores["brier"], 1.25/3)
        self.assertAlmostEqual(scores["calibration"], 0.5)

    def test_reject_invalid_policy_and_schedule(self):
        for changes in [dict(min_samples=True), dict(ks_threshold=0), dict(features={}),
                        dict(schema_version=1.0), dict(step_minutes=65)]:
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                policy(dict(self.cfg, **changes))
        for end, as_of in [("00", "02"), ("02", "01")]:
            with self.assertRaises(Invalid):
                window(self.cfg, "2026-01-08T" + end + ":00:00Z", "2026-01-08T" + as_of + ":00:00Z")
        with self.assertRaises(Invalid):
            window(self.cfg, "2026-01-08T01:30:00Z", "2026-01-08T02:00:00Z")

    def test_strict_input_contract(self):
        obs = dict(kind="observation", id="n1", available_at="2026-01-03T00:00:00Z",
                   event_at="2026-01-03T00:00:00Z", probability=0.5, features=dict(momentum=None, spread=2))
        for patch in [dict(features={}), dict(features=dict(momentum=False, spread=0)),
                      dict(probability=float("nan")), dict(probability=1.1), dict(id="../x"),
                      dict(extra=3), dict(event_at="2026-01-04T00:00:00Z")]:
            with self.subTest(patch=patch), self.assertRaises(Invalid):
                record(dict(obs, **patch), self.cfg)
        self.assertEqual(record(obs, self.cfg)["features"]["momentum"], None)
        for bad in ["2026-01-01", "2026-02-30T00:00:00Z", "2026-01-03T00:00:00+03:00"]:
            with self.assertRaises(Invalid):
                instant(bad)
