# ADR-001: Monotonic Fencing Tokens and Fail-Closed Resumption

- **Status:** Accepted (MVP)
- **Date:** 2026-11-27

## Context
During a network partition or process pause (GC pause, VM freeze), a primary trading node may lose contact with the coordinator while a standby promotes itself to primary. Without monotonic fencing, both nodes could submit orders concurrently (split-brain), causing duplicate executions and risk limit breaches.

## Decision
1. Every primary promotion increments a durable, strictly monotonic `Epoch` (`uint64`) managed by `fencing.Coordinator`.
2. Every order submission and broker reconciliation call passes `fencingEpoch` to both the local execution gate and the `BrokerAdapter` boundary.
3. Any request presenting `epoch < highest_observed_epoch` is rejected with `SPLIT_BRAIN_DETECTED`.
4. The execution gate (`EvaluateExecutionGate`) is strictly **fail-closed**: trading stays `DISABLED` unless fencing, snapshot/feed freshness, broker reconciliation, zero uncertain orders, and execution policy all pass simultaneously.

## Consequences
- Eliminates active-active split-brain order submission.
- Trades off availability for order safety during broker outages, network partitions, or stale data conditions (consistent with `plan.md` non-goals).
