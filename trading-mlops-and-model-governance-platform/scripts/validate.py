#!/usr/bin/env python3
"""Full CI-style validation script: verify fixtures, run tests, run demo, reproduce all evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=capture,
        check=True,
    )


def main() -> None:
    print("[validate] 1/4 Verifying fixtures...")
    _run([sys.executable, "scripts/generate_fixtures.py"])

    print("[validate] 2/4 Running full test suite (unit, integration, scenarios)...")
    test_proc = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        capture=True,
    )
    evidence_path = REPO_ROOT / "reports" / "test_evidence.txt"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        f"Command: python3 -m unittest discover -s tests -v\n\nSTDOUT:\n{test_proc.stdout}\nSTDERR:\n{test_proc.stderr}\n",
        encoding="utf-8",
    )
    print(test_proc.stderr.strip().splitlines()[-1] if test_proc.stderr.strip() else "Tests OK")

    print("[validate] 3/4 Running end-to-end demo...")
    _run([sys.executable, "scripts/demo.py"])

    print("[validate] 4/4 Reproducing M1-M4 and all 7 failure scenario reports...")
    _run([sys.executable, "scripts/reproduce.py"])

    scenarios = json.loads((REPO_ROOT / "reports" / "scenarios_evidence.json").read_text(encoding="utf-8"))
    assert scenarios["all_scenarios_passed"] is True
    assert scenarios["scenario_count"] == 7
    print("[validate] ALL CHECKS PASSED.")


if __name__ == "__main__":
    main()
