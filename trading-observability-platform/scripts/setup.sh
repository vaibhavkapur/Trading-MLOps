#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "Verifying Python runtime..."
python3 --version

echo "Verifying package importability via PYTHONPATH=src..."
PYTHONPATH="$ROOT_DIR/src" python3 -c "import trading_observability; print('trading_observability', trading_observability.__version__)"
echo "Setup complete. No external network dependencies are required for core operation."
