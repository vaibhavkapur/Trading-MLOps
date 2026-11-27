# ADR-003: Clean Contract Adapters and IP/Licensing Boundary

- **Status:** Accepted (MVP)
- **Date:** 2026-11-27

## Context
`plan.md` references four existing personal portfolio repositories (`High-Frequency-Trading-Engine`, `06-udp-multicast-feed`, `13-broker-reliability-reconciliation`, `07-trading-order-management-system`) and explicitly instructs confirming access, licenses, background IP, and SAAT ownership terms before copying or adapting them, falling back to clean documented contracts if unresolved.

## Decision
1. Implement the DR platform against clean, self-contained Go interfaces (`storage.ObjectStore`, `fencing.Coordinator`, `reconcile.BrokerAdapter`) and canonical domain schemas (`src/domain`).
2. Avoid copying code from external personal repositories in MVP scope.
3. Provide deterministic local adapters (`LocalObjectStore`, `FileCoordinator`, `SimulatedBroker`) and reference IaC (`Dockerfile`, `docker-compose.yml`, `config/postgres_wal.conf`) so clean checkouts run offline with zero external dependencies.

## Consequences
- Clean IP boundary and immediate offline reproducibility.
- Future integration with approved OMS, feed, or broker repositories requires writing thin adapters against the documented Go interfaces.
