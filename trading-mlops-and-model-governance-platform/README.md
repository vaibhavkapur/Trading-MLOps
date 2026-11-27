# Trading MLOps and Model Governance Platform

Reproducible training, dataset/feature lineage, multi-metric validation gates, model cards, RBAC shadow/paper promotion, integrity-verified rollback, drift monitoring, and tamper-evident audit controls for financial ML models and trading strategy artifacts.

> **Safety Notice:** Order execution is **disabled by default (`order_execution_enabled=false`)**. Automatic live promotion based on one metric and blind retraining on recent returns are strictly prohibited in code and configuration. Real-capital deployment requires separate legal, broker, security, and risk approval outside this MVP.

## Getting started

Requires only **Python 3.9+** (standard library only; zero mandatory external runtime dependencies).

```bash
# 1. Setup and generate deterministic synthetic fixtures
bash scripts/setup.sh

# 2. Run full validation (unit + integration + 7 failure scenario tests, demo, M1-M4 reports)
python3 scripts/validate.py

# 3. Run standalone end-to-end M1-M4 demo
python3 scripts/demo.py

# 4. Inspect audit chain and governance posture via CLI
PYTHONPATH=src python3 -m trading_mlops.cli --state-dir .tmp_run/reproduce_state verify-audit
PYTHONPATH=src python3 -m trading_mlops.cli --state-dir .tmp_run/reproduce_state governance-review
```

## Capabilities by Milestone

- **M1 — Foundation & Design**: Canonical immutable domain contracts (`DatasetSnapshot`, `FeatureSetContract`, `TrainingJobSpec`, `ExperimentRun`, `ValidationReport`, `ModelCard`, `ApprovalRecord`, `DeploymentRecord`, `RollbackRecord`), UTC timestamp & SemVer enforcement, RBAC roles, SQLite registry, hash-chained `AuditLog`.
- **M2 — Core Implementation**: Point-in-time & embargoed chronological split leakage guard, anti-blind-retrain enforcement, idempotent fixed-seed deterministic training with transaction costs, safe JSON artifact bundles with SHA-256 checksums + HMAC-SHA256 signatures, complete lineage DAG.
- **M3 — Integration & Validation**: 10-check multi-metric validation gate with bootstrap uncertainty, JSON + Markdown Model Cards, separation-of-duties independent approvals, strict `SHADOW -> PAPER -> LIVE_GATED` promotion with serving feature compatibility & integrity re-verification, and verified rollback.
- **M4 — Hardening & Delivery**: Feature PSI, mean z-shift, prediction drift, shadow-vs-champion divergence, structured alerts + Project #24 observability event export, Project #16 inference manifest export, governance review, and deployment/recovery drill.

## Repository Layout

```text
trading-mlops-and-model-governance-platform/
  plan.md                   # Implementation plan & acceptance checklist
  README.md                 # This file
  pyproject.toml            # Packaging & optional adapters
  requirements.lock         # Reproducible dependency lock
  config/                   # Versioned platform settings (no secrets)
  data/fixtures/            # Deterministic synthetic fixtures + SHA-256 manifest
  docs/                     # Architecture, ADRs, security/governance, runbook, limitations
  src/trading_mlops/        # Domain contracts, core modules, adapters, CLI
  tests/                    # unit/, integration/, scenarios/ (all 7 required failure cases)
  scripts/                  # setup.sh, generate_fixtures.py, demo.py, reproduce.py, validate.py
  reports/                  # Retained raw M1-M4, scenario, and test evidence
```

## Documentation

- [Implementation plan](plan.md)
- [Architecture & API/Schema Reference](docs/architecture.md)
- [Assumptions, IP Audit & Unsupported Features](docs/assumptions_and_limitations.md)
- [Security, RBAC & Governance Controls](docs/security_and_governance.md)
- [Operator Runbook & Recovery Drill](docs/runbook.md)
- [ADRs](docs/adrs/)
- [Validation & Acceptance Evidence Summary](reports/validation_summary.md)
