import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from tests.common import MonitorTest, ROOT
from scenarios import hour
from drift_monitor.contracts import Invalid, stamp
from drift_monitor.engine import evaluate, review, tick
from drift_monitor.storage import connect, ingest, register
from drift_monitor.views import dashboard, metrics


class Workflow(MonitorTest):
    def test_atomic_import_conflicts_and_watermark(self):
        obs, label = list(hour(self.t0, 71, count=1))
        with self.assertRaises(Invalid):
            ingest(self.db, self.name, [obs, dict(label, available_at=obs["event_at"])])
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0)
        ingest(self.db, self.name, [obs, label])
        self.assertEqual(ingest(self.db, self.name, [obs, label])["duplicates"], 2)
        with self.assertRaises(Invalid):
            ingest(self.db, self.name, [dict(obs, probability=0.1)])
        self.run_at(1, 1)
        with self.assertRaises(Invalid):
            ingest(self.db, self.name, [dict(obs, id="backdated")])
        with self.assertRaises(Invalid):
            register(self.db, dict(self.cfg, min_samples=200))

    def test_concurrent_retry_and_restart(self):
        self.add(-24)
        self.add(0, strength=3)
        end = stamp(self.t0 + timedelta(hours=1))
        def run(_):
            db = connect(self.path)
            try:
                return evaluate(db, self.name, end, end)
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(run, range(4)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM tickets").fetchone()[0], 1)
        self.assertEqual(tick(self.db, self.name, end), [])

    def test_reviews_authorized_and_audited(self):
        self.add(-24)
        self.add(0, strength=3)
        self.run_at(1, 1)
        with patch("drift_monitor.engine.local_actor", return_value="other"), self.assertRaises(Invalid):
            review(self.db, 1, "resolved", "wrong account")
        review(self.db, 1, "acknowledged", "<investigating>")
        review(self.db, 1, "acknowledged", "<investigating>")
        review(self.db, 1, "resolved", "feed corrected")
        with self.assertRaises(Invalid):
            review(self.db, 1, "acknowledged", "reopen")
        self.assertIn("&lt;investigating&gt;", dashboard(self.db))
        self.assertIn("drift_open_tickets 0", metrics(self.db))
        self.assertEqual(self.db.execute("SELECT count(*) FROM reviews").fetchone()[0], 2)

    def test_cli_batch_failure_and_readonly(self):
        args = [str(ROOT / "bin/drift"), "--db", str(self.path)]
        p = subprocess.run(args + ["ingest", "-", "--registry", self.name], input="garbage",
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stderr)["operation"], "ingest")
        self.assertIn('operation="ingest"} 1', subprocess.check_output(args + ["metrics"], text=True))
        db = connect(self.path, readonly=True)
        self.addCleanup(db.close)
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM registry")

    def test_tick_overlapping_windows_boundary_and_late_arrival(self):
        name = "overlap"
        register(self.db, dict(self.cfg, registry_id=name, step_minutes=30))
        self.assertEqual(len(tick(self.db, name, stamp(self.t0 + timedelta(minutes=90)))), 2)
        obs, label = list(hour(self.t0 + timedelta(hours=1), 70, count=1))
        obs["available_at"] = stamp(self.t0 + timedelta(minutes=100))
        ingest(self.db, name, [obs])
        ids = tick(self.db, name, stamp(self.t0 + timedelta(minutes=120)))
        ends = [self.db.execute("SELECT window_end FROM runs WHERE id=?", (i,)).fetchone()[0] for i in ids]
        self.assertEqual(ends, [stamp(self.t0 + timedelta(minutes=n)) for n in (90, 120)])

    def test_baseline_and_current_visibility(self):
        self.add(-24)
        obs, label = list(hour(self.t0 - timedelta(hours=24), 321, count=1))
        obs.update(id="late-baseline", available_at=stamp(self.t0 + timedelta(minutes=1)))
        ingest(self.db, self.name, [obs])
        self.add(0)
        obs, label = list(hour(self.t0, 322, count=1))
        obs.update(id="late-current", available_at=stamp(self.t0 + timedelta(minutes=61)))
        ingest(self.db, self.name, [obs])
        first, second = self.run_at(1, 1), self.run_at(1, 2)
        self.assertEqual(first["strata"][0]["baseline_count"], 80)
        self.assertEqual(first["strata"][0]["current_count"], 80)
        self.assertEqual(second["strata"][0]["current_count"], 81)
        self.assertEqual(first["baseline_sha256"], second["baseline_sha256"])
        self.assertEqual(first, self.run_at(1, 1))
