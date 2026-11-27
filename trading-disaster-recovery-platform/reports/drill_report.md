# Trading Disaster Recovery Platform — Automated Drill Evidence Report

- **Generated (UTC):** `2026-11-27T10:12:36Z`
- **Policy ID:** `saat-dr-policy-v1` (`local-prototype`)
- **Inventory ID:** `trading-dr-failure-modes-v1`
- **Fixed Seed:** `20260115`
- **Runtime:** `go1.27.1` (`darwin/arm64`)
- **Overall Result:** `true`

> Note: All measurements are local synthetic prototype drill measurements (not live production guarantees).

## Scenario Results & Measured RPO/RTO

| ID | Scenario | Component | Result | Fail-Closed | Proposed RPO (ms) | Measured RPO (ms) | Proposed RTO (ms) | Measured RTO (ms) |
|---|---|---|---|---|---:|---:|---:|---:|
| FM-001 | `process_crash_mid_submit` | `oms_state` | **PASS** | true | 0 | 0 | 5000 | 5.106 |
| FM-002 | `duplicate_events` | `oms_state` | **PASS** | true | 0 | 0 | 5000 | 4.410 |
| FM-003 | `database_restore` | `database_backup_restore` | **PASS** | true | 1000 | 0 | 30000 | 3.107 |
| FM-004 | `network_partition` | `broker_gateway_failover` | **PASS** | true | 0 | 0 | 15000 | 0.184 |
| FM-005 | `split_brain` | `broker_gateway_failover` | **PASS** | true | 0 | 0 | 15000 | 4.633 |
| FM-006 | `stale_snapshot` | `market_data_snapshot` | **PASS** | true | 5000 | 0 | 10000 | 4.846 |
| FM-007 | `backup_corruption` | `database_backup_restore` | **PASS** | true | 1000 | 0 | 30000 | 3.157 |
| FM-008 | `broker_outage` | `broker_gateway_failover` | **PASS** | true | 0 | 0 | 15000 | 0.143 |

## Scenario Verification Evidence

### FM-001 — `process_crash_mid_submit`

Recovered both broker-filled and broker-absent mid-submit orders without duplicate submission.

- `in_flight_marked_unknown=[ORD-1004 ORD-1005]`
- `ORD-1004_converged=FILLED (AAPL_pos=70)`
- `ORD-1005_not_resubmitted_status=REJECTED (broker_submits=0)`
- `reconciled_cash_minor=96607715 (expected=96607715)`

### FM-002 — `duplicate_events`

Exact-replay and redelivered fill duplicates were idempotently ignored with zero balance drift.

- `duplicates_ignored=3`
- `distinct_orders=3 distinct_fills=4`
- `reconciled_cash=96793740 AAPL=60 MSFT=50`

### FM-003 — `database_restore`

Restored OMS and ledger from verified gzip/SHA-256 checkpoint plus WAL tail and reconciled with broker.

- `backup_sha256_verified=cea1000fd16144ea`
- `wal_tail_replayed=seq_8_to_13`
- `restored_cash=96793740 AAPL=60 MSFT=50`

### FM-004 — `network_partition`

Network partition fenced order submission fail-closed; healing and broker reconciliation restored trading.

- `partitioned_gate_allowed=false`
- `partitioned_submit_blocked=true (broker_submits=0)`
- `post_heal_reconciled_gate_allowed=true`

### FM-005 — `split_brain`

Monotonic fencing epoch blocked stale primary at both local gate and broker boundary.

- `epoch_transition=1->2`
- `stale_primary_blocked_locally=true`
- `stale_primary_fenced_at_broker=true`
- `authorized_standby_order=BRK-9101 (total_broker_submits=1)`

### FM-006 — `stale_snapshot`

Stale checkpoint and stale market feed blocked execution fail-closed.

- `freshness_passed=false gate_allowed=false`
- `blocking_reasons=[stale_checkpoint_snapshot: age=120000ms > max=30000ms stale_market_feed: age=122613ms > max=5000ms]`
- `order_blocked_fail_closed=true`

### FM-007 — `backup_corruption`

SHA-256 backup payload check and WAL hash-chain check aborted corrupted restore fail-closed.

- `backup_sha256_corruption_detected=true`
- `wal_hash_chain_tamper_detected=true`
- `gate_remains_closed=true`

### FM-008 — `broker_outage`

Broker outage kept execution gate locked (fail-closed); resumption allowed only after broker reconciliation succeeded.

- `broker_outage_blocked_unknown_state=true`
- `outage_order_submission_blocked=true`
- `post_outage_reconciled_gate_allowed=true`

