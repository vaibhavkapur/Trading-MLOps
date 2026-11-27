# Operator Runbook — Trading Disaster Recovery Platform

## 1. Core Operating Principle: Fail-Closed First

- **Never bypass fencing or broker reconciliation** to resume trading faster.
- **Never enable live execution** (`live_execution_allowed` must stay `false` until independent legal, risk, broker, and security sign-offs are recorded).
- When any safety check fails, the execution gate defaults to `DISABLED` (`allowed=false`). Treat a blocked gate as the **safe state**.

## 2. Standard Failover & Recovery Procedure

1. **Confirm Incident Scope:** Identify affected component (`oms_state`, `position_and_cash_ledger`, `market_data_snapshot`, `database_backup_restore`, `broker_gateway_failover`) and map to `FM-001`..`FM-008`.
2. **Verify Backup & WAL Integrity:**
   ```bash
   ./bin/drctl verify-backup --store <object-store-dir> --backup-id <backup-id>
   ./bin/drctl verify-wal --wal <wal-file-path>
   ```
3. **Fence Old Primary:** Wait for the previous primary lease TTL (`lease_ttl_ms`) to expire (or confirm primary termination/isolation), then promote standby to monotonic `epoch + 1`.
4. **Restore & Replay:** Restore the latest verified checkpoint backup, replay the hash-chained WAL tail, and automatically transition any `PENDING_SUBMIT` orders to `UNKNOWN`.
5. **Broker Reconciliation:** Query the broker with the new `epoch` to resolve all `UNKNOWN` orders and verify zero cash/position discrepancy against the broker snapshot.
6. **Verify Execution Gate:** Confirm `gate_status.allowed == true`, `mode == PAPER_SANDBOX`, `fence_valid == true`, `freshness_passed == true`, and `reconciliation_passed == true`.

## 3. Failure-Mode Response Matrix & Manual Escalation

| ID | Failure Mode | Automatic Fail-Closed Action | Operator Action & Manual Escalation Trigger |
|---|---|---|---|
| `FM-001` | Process crash mid-submit | Pending orders marked `UNKNOWN`; resolved via broker query without resubmission. | Verify `resolutions` report; if any order remains `UNKNOWN`, keep trading halted and contact broker desk. |
| `FM-002` | Duplicate events | Duplicates ignored via `event_id`, `idempotency_key`, and `fill_id`. | Inspect `duplicate_events_ignored` / `duplicate_fills_ignored` metrics; investigate upstream redelivery storm if sustained. |
| `FM-003` | Database restore | Restores SHA-256 verified backup + WAL tail and runs broker reconciliation. | Compare restored ledger against broker statement; escalate to CTO/Risk if any balance diff is non-zero. |
| `FM-004` | Network partition | Lease validation fails; execution gate blocks all orders immediately. | Do **not** force-override gate. Restore network connectivity, re-validate lease/epoch, and run full broker reconciliation. |
| `FM-005` | Split brain | Stale primary rejected with `SPLIT_BRAIN_DETECTED` at local gate and broker gateway. | Immediately terminate stale primary process, audit logs for attempted submissions, confirm single active epoch holder. |
| `FM-006` | Stale snapshot | `STALE_SNAPSHOT` blocks trading when checkpoint or market feed exceeds freshness TTL. | Restore market data feed, confirm fresh `available_time_utc`, and take a new verified checkpoint before resuming. |
| `FM-007` | Backup corruption | Restore aborts immediately with `BACKUP_CORRUPTED` or `WAL_CORRUPTED`; gate stays closed. | **Escalate immediately to Security & Infrastructure Engineering.** Do not disable checksum checks. Roll back to prior clean verified backup and reconcile against broker ground truth. |
| `FM-008` | Broker outage | Reconciliation fails with `UNKNOWN_EXTERNAL_STATE`; gate stays closed. | **Escalate to Broker Operations.** Monitor broker status; only resume after broker connectivity recovers and reconciliation passes cleanly. |

## 4. Escalation Contacts & Roles (To Be Populated Before Sandbox Rollout)

- **Primary On-Call:** Trading Engineering Lead (owns fencing, replay, backup verification)
- **Risk Escalation:** Risk / Operations Officer (required for any cash/position reconciliation mismatch > 0)
- **Security / Infra Escalation:** Security & Infrastructure Owner (required for `BACKUP_CORRUPTED` or `WAL_CORRUPTED` alerts)
- **Executive Approval:** CTO (required for any policy change or transition from local prototype to paper/sandbox environment)

## 5. Tabletop & Automated Drill Cadence

- Run `./scripts/validate.sh` on every code or policy change to execute all 8 automated failure scenarios and regenerate `reports/drill_results.json` and `reports/drill_report.md`.
- Conduct periodic tabletop walkthroughs simulating multi-failure combinations (e.g., database restore coinciding with broker outage) and document unresolved gaps in `docs/assumptions-and-limitations.md`.
