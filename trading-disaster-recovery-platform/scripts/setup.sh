#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Checking Go toolchain"
go version

echo "==> Verifying module & building drctl"
mkdir -p bin
go build -o bin/drctl ./src/cmd/drctl
echo "Build succeeded: bin/drctl"
