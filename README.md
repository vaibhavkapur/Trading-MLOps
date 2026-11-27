# Trading MLOps

Four independent projects use synthetic trading data to test how models are approved, how operators detect drift and trace orders, and when a trading system can safely resume after a failure. The model governance, drift detection, and observability projects use Python. The disaster recovery platform uses Go.

## Getting started

Start with the Trading MLOps and Model Governance Platform. Its offline demo requires Python 3.9 or later and uses the standard library.

```bash
git clone https://github.com/vaibhavkapur/Trading-MLOps.git
cd Trading-MLOps/trading-mlops-and-model-governance-platform
bash scripts/setup.sh
python3 scripts/demo.py
```

The demo trains a model, promotes it through shadow and paper review, and checks it against a shifted dataset. Inspect `m4_drift_severity` and `m4_recommended_safe_fallback` in the output to see what the monitor detected and how it recommends responding. Run each project's setup, demo, and validation commands from its own directory.

## Projects

- [Trading MLOps and Model Governance Platform](trading-mlops-and-model-governance-platform/) — Tracks training data and model lineage, signs artifacts, and requires independent approval before promotion. Its recovery drill verifies the artifact before rollback.
- [Financial Model Drift Detection Platform](financial-model-drift-detection-platform/) — Compares features and forecasts with a frozen baseline, scores predictions as labels arrive, and records the evidence behind each review ticket.
- [Trading Observability Platform](trading-observability-platform/) — Follows a paper order across six services using correlated traces, redacted logs, and Prometheus metrics. Fault scenarios exercise alert routing and monitoring outages.
- [Trading Disaster Recovery Platform](trading-disaster-recovery-platform/) — Restores a checkpoint, replays the write-ahead log, and reconciles orders, cash, and positions with a simulated broker before resuming paper trading.

Each project README links to its architecture, operator runbook, and validation evidence.
