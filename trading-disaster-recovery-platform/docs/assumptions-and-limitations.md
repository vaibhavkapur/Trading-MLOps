# Assumptions, Dependency Audit, Security Review, and Known Limitations

## 1. Upstream Foundation Audit & IP/Licensing Boundary

`plan.md` lists four existing portfolio repositories as integration dependencies:
- `vaibhavkapur/High-Frequency-Trading-Engine`
- `vaibhavkapur/High-Frequency-Trading` (`projects/06-udp-multicast-feed`)
- `vaibhavkapur/Trading-Software` (`projects/13-broker-reliability-reconciliation`)
- `vaibhavkapur/Trading-Software` (`projects/07-trading-order-management-system`)

**Audit Outcome (M1):**
Per `plan.md` Section 2 ("Confirm access, licenses, background IP and SAAT ownership terms before copying or adapting any personal repository... If access or licensing is unresolved, implement against clean, documented contracts instead"):
- No code was copied from external personal repositories.
- Clean, documented Go contracts were defined in `src/domain`, `src/oms`, `src/ledger`, `src/storage`, `src/fencing`, and `src/reconcile` so that future adapters can integrate existing OMS, multicast feed, or broker reconciliation implementations once IP, licensing, and pinned commit audits are formally approved.

## 2. Security, Data Licensing, and Deployment Governance Review

- **Data Provenance:** All fixtures in `data/fixtures/` are 100% synthetic (documented in `data/fixtures/manifest.json`, fixed seed `20260115`). No licensed third-party market data or real brokerage records are stored.
- **Secrets Handling:** Zero credentials are stored in source or `config/`. `docker-compose.yml` requires external environment variables (`DR_PG_PASSWORD`, `DR_OBJ_PASSWORD`).
- **Execution Control:** `config/dr_policy.v1.json` enforces `live_execution_allowed: false` and `domain.DRPolicy.Validate()` rejects any configuration attempting to enable live execution in MVP scope.
- **Deployment Gate:** Promoting from local prototype to an approved paper/sandbox environment requires separate confirmation of SAAT asset class, strategy horizon, broker API credentials, and infrastructure approval.

## 3. Explicit List of Unsupported Features & Unresolved Failure Modes

To maintain full transparency (no unmeasured guarantees or silent assumptions):

1. **No Active-Active Order Submission:** Explicitly forbidden by design; only single-primary fenced failover is supported.
2. **No Distributed Consensus Cluster in Prototype Runtime:** `fencing.FileCoordinator` provides atomic local lock/epoch semantics for single-host prototype verification. Multi-host deployment requires replacing `FileCoordinator` with an etcd/Consul/Raft-backed `fencing.Coordinator` implementation and verifying clock-skew bounds.
3. **No Live PostgreSQL PITR Execution in Offline Unit Tests:** `config/postgres_wal.conf` and `docker-compose.yml` provide the reference PostgreSQL 16 WAL + MinIO topology, while the deterministic prototype tests exercise the Go WAL + `LocalObjectStore` backup manager directly so tests run cleanly offline without Docker.
4. **No Real Broker API Integration:** `reconcile.SimulatedBroker` implements the `BrokerAdapter` contract for deterministic fault-injection drills. Real broker adapters (FIX/REST/WebSocket) must implement broker-side client-order-ID idempotency and fencing-epoch validation (or external order-cancel-on-disconnect).
5. **Unresolved Failure Modes Requiring Future Hardening:**
   - **Byzantine / Silent Broker Fill Omission:** If an external broker omits a fill from its own snapshot API temporarily (eventual consistency), reconciliation could falsely flag cash drift or delay resumption; requires broker sequence-number watermarks.
   - **Host Clock Drift Exceeding `clock_skew_tolerance_ms`:** Wall-clock TTL leases assume bounded NTP/PTP drift; severe clock jumps on a partitioned primary require broker-side fencing tokens (which our `BrokerAdapter` already enforces via `fencingEpoch`) rather than relying solely on local clocks.
   - **Multi-Currency FX Settlement & Corporate Actions:** Current ledger tracks exact minor-unit balances per `(account_id, currency)` and symbol positions, without FX conversion or dividend/split adjustments.
   - **Partial Uncommitted In-Memory Events Before WAL `fsync`:** RPO=0 in drills reflects committed WAL events; events received in memory before WAL `fsync` completes can still be lost on sudden power failure.
