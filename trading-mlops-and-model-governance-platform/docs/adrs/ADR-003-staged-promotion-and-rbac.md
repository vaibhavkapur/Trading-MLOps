# ADR-003: Staged Promotion State Machine, Multi-Metric Gate, and Separation of Duties

- **Status:** Accepted
- **Milestone:** M3

## Context
Promoting financial models automatically based on a single backtest metric (e.g., gross Sharpe) or allowing researchers to self-approve live releases causes hidden tail risk, overfit turnover, and governance failures.

## Decision
1. Enforce a 10-check multi-metric validation gate incorporating transaction costs, max drawdown, turnover, hit rate, IC, sub-period stability, sample size, and bootstrap uncertainty.
2. Enforce strict stage progression (`REGISTERED -> VALIDATED -> SHADOW -> PAPER -> LIVE_GATED`) with no stage skipping.
3. Enforce RBAC and separation of duties (`approver_id != artifact.created_by`), dual independent sign-off (`RISK_APPROVER` + `COMPLIANCE_OFFICER`) for `LIVE_GATED`, serving feature version compatibility checks, and `order_execution_enabled=false` default.

## Consequences
- Eliminates single-metric promotion and unauthorized or self-approved releases.
- Adds human review latency by design, matching institutional risk governance requirements.
