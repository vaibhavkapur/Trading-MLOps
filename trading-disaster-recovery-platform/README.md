# Trading Disaster Recovery Platform

Local reproducible MVP implementation of the **Trading Disaster Recovery Platform** (`plan.md`, Project 25 of 25).

Recovers trading state and operations after process, feed, datastore, network, or broker failures while preserving order safety, monotonic primary fencing, and reconciled cash/position balances.

> **Operational Safety Notice:** Live capital order execution is strictly **disabled** in code and versioned policy (`live_execution_allowed: false`). Only local/sandbox paper execution is permitted after fencing, freshness checks, and broker reconciliation succeed. This prototype is not an assertion of production readiness.

## Key Capabilities

- **Failure-Mode Inventory & Recovery Objectives** (`config/failure_modes.v1.json`, `config/dr_policy.v1.json`): Versioned domain contracts and component-level proposed RPO/RTO targets.
- **Durable Checkpoints & Tamper-Evident WAL** (`src/storage/`): Append-only JSONL WAL with SHA-256 hash chaining and `fsync`, plus atomic gzipped checkpoint backups with SHA-256 manifest verification over a pluggable `ObjectStore` interface.
- **Deterministic Idempotent Replay** (`src/replay/`, `src/oms/`, `src/ledger/`): Exact integer minor-unit accounting (ISO-4217 currency), deduplication by `event_id`, `idempotency_key`, and `fill_id`, journal-to-state invariant verification, and crash recovery marking in-flight `PENDING_SUBMIT` orders as `UNKNOWN`.
- **Fenced Failover & Broker Reconciliation** (`src/fencing/`, `src/reconcile/`, `src/dr/`): Monotonic fencing epochs preventing split-brain at both local gate and broker boundary, stale-snapshot/stale-feed freshness blocks, and mandatory broker reconciliation before resuming.
- **Automated Drills & Evidence Generation** (`src/cmd/drctl/`, `scripts/validate.sh`, `reports/`): Deterministic verification of all 8 required failure scenarios with measured RPO/RTO evidence.

## Getting started

Requires **Go 1.21+** (zero external module dependencies).

```bash
# 1. Verify environment and build CLI
./scripts/setup.sh

# 2. Run reproducible end-to-end failover & fencing demo
./scripts/demo.sh

# 3. Run full validation (go vet, go test -v -race, all 8 DR drills, generate reports)
./scripts/validate.sh
```

Or via `Makefile`:

```bash
make validate
make demo
```

## Repository Layout

```text
trading-disaster-recovery-platform/
  plan.md
  README.md
  go.mod
  Makefile
  Dockerfile
  docker-compose.yml            # Reference local sandbox PostgreSQL WAL + MinIO topology
  config/
    dr_policy.v1.json           # Versioned DR & fail-closed execution policy
    failure_modes.v1.json       # Canonical 8-scenario failure-mode inventory
    postgres_wal.conf           # Reference PostgreSQL 16 WAL archiving config
  data/fixtures/                # Synthetic fixtures, broker baseline, provenance manifest
  docs/                         # Architecture, ADRs, API/schemas, runbook, limitations
  src/
    cmd/drctl/                  # CLI: demo, drills, verify-wal, verify-backup
    domain/                     # Canonical types, structured errors, policy validation
    ledger/                     # Deterministic cash/position ledger & journal invariants
    oms/                        # Order state machine, idempotency registry, uncertain orders
    storage/                    # Hash-chained WAL & SHA-256 verified backup manager
    replay/                     # Deterministic checkpoint + WAL replay engine
    fencing/                    # Monotonic epoch lease coordinator
    reconcile/                  # Broker adapter contract & reconciliation engine
    dr/                         # Failover coordinator & 8-scenario automated drill runner
  tests/
    unit/                       # Domain, ledger, OMS, WAL, backup, fencing invariants
    integration/                # End-to-end restore/replay/reconcile & drift-blocking tests
    scenarios/                  # All 8 required failure-path scenarios
  scripts/                      # setup.sh, demo.sh, validate.sh
  reports/                      # Retained drill evidence (JSON + MD) and test report
```

## Documentation

- [Implementation plan](plan.md)
- [Architecture & Component Boundaries](docs/architecture.md)
- [API, Domain Contracts & Schemas](docs/api-and-schemas.md)
- [Operator Runbook & Manual Escalation](docs/runbook.md)
- [Assumptions, Upstream Audit, Security & Known Limitations](docs/assumptions-and-limitations.md)
- Architecture Decision Records:
  - [ADR-001: Monotonic Fencing & Fail-Closed Resumption](docs/adr/ADR-001-fencing-and-fail-closed.md)
  - [ADR-002: Hash-Chained WAL & SHA-256 Verified Checkpoints](docs/adr/ADR-002-wal-checkpoint-and-checksum-verification.md)
  - [ADR-003: Clean Contract Adapters & IP/Licensing Boundary](docs/adr/ADR-003-clean-contract-adapters-and-ip-boundary.md)
- Validation Evidence:
  - [Automated Drill Report](reports/drill_report.md)
  - [Raw Drill JSON Evidence](reports/drill_results.json)
  - [Go Race Test Report](reports/test_report.txt)
