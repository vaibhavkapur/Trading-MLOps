# ADR-001: Canonical Domain Contracts and SQLite-First Registry

- **Status:** Accepted
- **Milestone:** M1

## Context
Financial MLOps requires unambiguous lineage between datasets, feature contracts, code commits, experiments, validation reports, approvals, and deployments before adopting distributed infrastructure.

## Decision
1. Define immutable Python dataclasses in `src/trading_mlops/contracts.py` with strict UTC ISO-8601 timestamps, strict SemVer versions, explicit units/currency, and content-addressed IDs (`<id>@<semver>+<sha16>`) aligned with Project #18 (Financial Feature Store).
2. Persist canonical metadata in a transactional local SQLite database (`SQLiteRegistryStore`) with explicit uniqueness/immutability constraints and idempotency keys, keeping PostgreSQL isolated behind `PostgresRegistryStubAdapter`.

## Consequences
- Zero external infrastructure required for clean local checkout and deterministic CI verification.
- Migrating to PostgreSQL only requires implementing the same persistence methods over `psycopg`.
