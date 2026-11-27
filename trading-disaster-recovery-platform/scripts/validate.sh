#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p bin reports

echo "==> 1. Building drctl"
go build -o bin/drctl ./src/cmd/drctl

echo "==> 2. Running go vet"
go vet ./...

echo "==> 3. Running unit, integration, and scenario tests with race detector"
go test -count=1 -v -race ./... | tee reports/test_report.txt

echo "==> 4. Running all 8 automated DR drills and writing evidence reports"
./bin/drctl drills \
  --policy config/dr_policy.v1.json \
  --failure-modes config/failure_modes.v1.json \
  --fixtures data/fixtures/baseline_events.jsonl \
  --broker data/fixtures/broker_baseline.json \
  --out-json reports/drill_results.json \
  --out-md reports/drill_report.md

echo "==> 5. Running reproducible demo"
./scripts/demo.sh

echo "==> Validation complete. Evidence written to reports/"
