import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "data/fixtures")]
from drift_monitor.contracts import instant, stamp
from drift_monitor.engine import evaluate
from drift_monitor.storage import connect, ingest, register
from drift_monitor.views import evidence
from scenarios import hour


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "monitor.sqlite3"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.cfg = json.loads((ROOT / "config/monitor.v1.json").read_text())
        self.name = self.cfg["registry_id"]
        self.t0 = instant(self.cfg["baseline_end"])
        register(self.db, self.cfg)

    def add(self, n, scenario="stable", strength=0, count=80, seed=50):
        ingest(self.db, self.name, hour(self.t0 + timedelta(hours=n), seed, scenario, strength, count))

    def run_at(self, end, as_of):
        run = evaluate(self.db, self.name, stamp(self.t0 + timedelta(hours=end)),
                       stamp(self.t0 + timedelta(hours=as_of)))
        return evidence(self.db, run)

    def checks(self, run):
        return {c["signal"]: c for c in run["strata"][0]["checks"]}
