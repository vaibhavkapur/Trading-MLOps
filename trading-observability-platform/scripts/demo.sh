#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "Running Trading Observability Platform end-to-end paper-trade demo..."
PYTHONPATH="$ROOT_DIR/src" python3 -m trading_observability demo --orders 10 --seed 20261127 --reports-dir reports
