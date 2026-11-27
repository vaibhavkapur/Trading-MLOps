#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== 1. Running unit, integration, and scenario test suite ==="
PYTHONPATH="$ROOT_DIR/src" python3 -m unittest discover -s tests -t . -v

echo "=== 2. Running reproducible fault scenarios & local synthetic load baseline ==="
PYTHONPATH="$ROOT_DIR/src" python3 -m trading_observability scenarios --seed 20261127 --load-orders 200 --reports-dir reports

echo "=== 3. Running end-to-end paper-trade demo ==="
PYTHONPATH="$ROOT_DIR/src" python3 -m trading_observability demo --orders 10 --seed 20261127 --reports-dir reports

echo "Validation complete. Evidence written to reports/."
