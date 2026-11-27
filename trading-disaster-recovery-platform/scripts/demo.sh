#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x bin/drctl ]]; then
  ./scripts/setup.sh
fi

./bin/drctl demo \
  --policy config/dr_policy.v1.json \
  --fixtures data/fixtures/baseline_events.jsonl \
  --broker data/fixtures/broker_baseline.json
