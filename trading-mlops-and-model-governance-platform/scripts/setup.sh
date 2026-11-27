#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "[setup] Python version: $(python3 --version)"
echo "[setup] Generating deterministic synthetic fixtures..."
python3 scripts/generate_fixtures.py
echo "[setup] Verifying package import..."
PYTHONPATH=src python3 -c "import trading_mlops; print('[setup] Imported trading_mlops successfully')"
echo "[setup] Setup complete. Run 'python3 scripts/validate.py' to run tests and generate evidence."
