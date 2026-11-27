# Trading Observability Platform

Unified trading telemetry platform across **market data**, **research**, **risk**, **OMS**, **broker**, and **portfolio** services with W3C distributed trace correlation, bounded-cardinality Prometheus metrics, mandatory sensitive-field redaction, deduplicated alert routing, independent dead-man's-switch monitoring, and reproducible fault-scenario evidence.

> **Status & Scope Notice:** This repository is a reproducible local/sandbox MVP prototype built with permitted synthetic fixtures. It does **not** assert production readiness, does **not** connect to live brokers or real capital, and does **not** claim production latency or uptime SLAs. All SLO targets in `config/slis.json` are explicitly marked as **PROPOSED** for sandbox calibration.

## Getting started

No network access or external runtime dependencies are required for core operation (Python 3.9+ standard library):

```bash
# 1. Verify environment and package importability
bash scripts/setup.sh

# 2. Run all unit, integration, and fault scenario tests + generate reports/
bash scripts/validate.sh

# 3. Run the end-to-end paper-trade observability demo standalone
bash scripts/demo.sh

# 4. (Optional) Start the local HTTP metrics, dashboard, alerts & trace server
PYTHONPATH=src python3 -m trading_observability serve --host 127.0.0.1 --port 9108
```

Endpoints when running `serve`:
- `GET /metrics` — Prometheus exposition format (`text/plain; version=0.0.4`)
- `GET /health` and `GET /ready` — Collector and trace backend liveness/readiness
- `GET /dashboard` — Trading health, latency histograms, SLI evaluation, and observability self-health
- `GET /alerts` — Active alerts and routed incident notifications
- `GET /traces/<order_id>` — Role-checked, redacted order trace (`VIEWER` default; `AUDITOR_AUTHORIZED` requires `X-Auditor-Token` matching `TRADING_OBS_AUDITOR_TOKEN`)

Optional Docker stack (`docker-compose.yml`) wires real **Prometheus**, **Alertmanager**, **OpenTelemetry Collector**, and **Grafana** against the local `/metrics` endpoint and versioned dashboard/alert rules in `config/`.

## Repository Layout

```text
trading-observability-platform/
  plan.md                                 # Implementation specification
  README.md                               # Quickstart, layout, and unsupported features
  pyproject.toml                          # Package metadata and optional OTel/dev extras
  docker-compose.yml                      # Optional Prometheus + Alertmanager + OTel + Grafana stack
  config/
    platform.json                         # Versioned runtime settings, cardinality caps, execution policy (default DISABLED)
    slis.json                             # Canonical SLI catalog with PROPOSED targets
    prometheus/{prometheus.yml,alerts.yml}
    alertmanager/alertmanager.yml
    otel/otel-collector-config.yaml
    grafana/dashboards/{trading-health,trading-latency,observability-self-health}.json
  data/fixtures/
    manifest.json                         # Provenance and entitlement manifest (100% synthetic)
    synthetic_ticks.jsonl                 # Deterministic market ticks (UTC event & availability times)
  src/trading_observability/
    config.py                             # Versioned config & fail-closed ExecutionPolicy gate
    schema.py                             # W3C TraceContext, CorrelationContext, Span/Log/Alert/Error contracts
    redaction.py                          # Sensitive-field & confidential-order scrubbing firewall + RBAC projection
    metrics.py                            # Bounded-cardinality Prometheus registry + overflow protection
    logging_service.py                    # Structured JSON logger with mandatory redaction
    tracing.py                            # SQLite trace store, OTLP/JSON export, delayed-trace detection, spool & retention
    alerting.py                           # Policy engine, fingerprint dedup/cooldown, router, and MonitoringWatchdog
    dashboards.py                         # Trading health, latency, SLI, and self-health dashboard snapshot builder
    adapters.py                           # Clean adapter Protocols & deterministic fault-injectable synthetic adapters
    pipeline.py                           # 6-service instrumented paper-trade pipeline + ObservabilityPlatform facade
    scenarios.py                          # All 7 plan.md fault scenarios + M3/M4 drill + local synthetic load baseline
    server.py                             # Local HTTP endpoint server
    cli.py                                # CLI (`demo`, `scenarios`, `serve`)
  tests/
    unit/                                 # Schema, redaction, RBAC, policy gate, cardinality, tracing, retention
    integration/                          # 6-service pipeline, idempotency, HTTP endpoints, config artifact validation
    scenarios/                            # All 7 required failure paths + M3/M4 drill + load baseline
  scripts/                                # setup.sh, demo.sh, validate.sh, reproduce.sh
  docs/                                   # Architecture, telemetry schema, ADR-001, assumptions, operator runbooks
  reports/                                # Generated validation reports, demo snapshots, OTLP trace & Prometheus output
```

## Documentation

- [Implementation plan](plan.md)
- [Architecture Overview](docs/architecture.md)
- [Telemetry Schema & Redaction Specification](docs/telemetry-schema.md)
- [ADR-001: Telemetry Boundaries, Cardinality & Fail-Safe Design](docs/adr-001-telemetry-boundaries.md)
- [Assumptions, External Dependencies & IP/Security Review](docs/assumptions-and-dependencies.md)
- [Operator Incident Runbooks](docs/runbooks.md)
- [Scenario Validation & Local Load Baseline Report](reports/scenario_validation_report.md)
- [Acceptance Criteria Evidence](reports/acceptance_evidence.md)

## Explicit List of Unsupported Features (MVP Scope)

1. **Live Broker / Real-Capital Execution:** Hard-prohibited by `ExecutionPolicyConfig` (`LIVE` raises `PermissionError`; default is `DISABLED`).
2. **External Proprietary Market Data Vendors:** Not connected; only synthetic permitted fixtures are used until data entitlements are confirmed.
3. **Production High-Availability Cluster Deployment:** Single-node SQLite trace store and in-process registry are for local/sandbox prototyping; production requires managed Tempo/Jaeger, Prometheus/Mimir, and external secret management.
4. **Live PagerDuty / Slack Webhook Credentials:** Alertmanager config uses localhost placeholders; real receiver secrets must be injected externally.
5. **Production Latency / Uptime SLAs:** Not claimed; all targets are explicitly labeled `PROPOSED` or `MEASURED_LOCAL_SYNTHETIC_BASELINE`.
