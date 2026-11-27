# Financial Model Drift Detection Platform

A local monitor for versioned binary forecasts. It compares numeric features and predicted probabilities with a frozen reference, evaluates accuracy and calibration when labels mature, and opens evidence-backed tickets for human review. The worked example uses synthetic 10-minute forecasts.

## Run the prototype

From this directory on macOS or Linux with Python 3.9+ and SQLite 3.24+ built into Python, run the following commands. The runtime and tests use only the standard library.

```sh
scripts/setup
scripts/validate
scripts/demo
```

The demo prints paths to a standalone HTML dashboard, a SQLite database and a Prometheus textfile inside a fresh private directory under `var/demos/`. Open the HTML file locally, expand an incident, and inspect the trigger, window counts and review history. Each invocation builds an independent example.

## Use your own local registry

```sh
export DRIFT_DB="$PWD/var/example.sqlite3"
bin/drift register config/monitor.v1.json
scripts/fixtures > var/events.ndjson
bin/drift ingest var/events.ndjson --registry direction-hourly-v1
bin/drift tick --registry direction-hourly-v1 --as-of 2026-01-08T07:00:00Z
bin/drift show 1
bin/drift dashboard > var/dashboard.html
bin/drift metrics > var/drift.prom
```

A tick evaluates every due window and refreshes older windows when newly available observations, labels or cohort maturity can change their evidence. The explicit cutoff keeps a historical replay on the fixture clock. Register a fresh identifier and feed when changing the baseline or policy.

## Validation and handoff

- [Chronological evaluation and acceptance evidence](reports/acceptance.md)
- [Raw tuning and holdout windows](reports/evaluation.json) and [test output](reports/tests.txt)
- [Architecture, decisions and limitations](docs/architecture.md)
- [Record schema, scoring and CLI contract](docs/contracts.md)
- [Worker operation, incident review, access and recovery](docs/runbook.md)
- [Synthetic fixture provenance](data/fixtures/manifest.json)

Run `scripts/ci` to execute the same checks as the repository workflow, including replay and comparison with the retained evidence. Local output stays under `var/`. This prototype ends at a human review ticket. Connecting a provider or a deployment system requires a separate project decision.
