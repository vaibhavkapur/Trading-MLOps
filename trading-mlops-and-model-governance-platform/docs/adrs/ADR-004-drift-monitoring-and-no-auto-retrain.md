# ADR-004: Drift Monitoring, Hash-Chained Audit Trail, and No Blind Auto-Retraining

- **Status:** Accepted
- **Milestone:** M4

## Context
Market regime shifts cause feature and prediction distributions to drift. Automatically retraining on a short window of recent returns amplifies regime-chasing and silently deploys unvetted models.

## Decision
1. Compute deterministic feature Population Stability Index (PSI), mean z-score shift, prediction drift, and shadow-vs-champion disagreement, emitting structured alerts (`INFO`/`WARNING`/`CRITICAL`) compatible with Project #24 (Trading Observability).
2. Explicitly prohibit blind retraining (`allow_blind_recent_return_retraining = false`, minimum 30-day multi-regime span) and automatic live promotion (`allow_automatic_live_promotion = false`).
3. On `CRITICAL` drift, recommend freezing promotion and rolling back to a verified prior version (`order_execution_enabled=false`) rather than auto-retraining.
4. Record every lifecycle action, blocked attempt, drift alert, and rollback in an append-only SHA-256 hash-chained `audit_log.jsonl`.

## Consequences
- Operators receive deterministic, actionable drift alerts and a verified rollback path without risking uncontrolled model churn.
