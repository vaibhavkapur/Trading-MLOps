# Assumptions, Dependency Audit, and Known Limitations

## 1. Existing Foundations & IP / Licensing Audit

The implementation plan references three external personal repositories as conceptual foundations:
1. `Machine-Learning — Sagemaker-Fraud-Detector`
2. `Trading-Software — 14-ai-quant-research-agent`
3. `Quantum-Trading — 07-quantum-order-book-predictor`

**Clean-Room Implementation Decision:**
Because access, licenses, background IP transfer, and SAAT ownership terms for those external personal repositories have not yet been formally confirmed—and linked prototypes are not proof of production readiness—this MVP implements **clean, self-contained, documented contracts** from scratch with zero copied external code. Adapters can be connected later once IP, licensing, and pinned commit audits are completed.

## 2. Modeling and Evaluation Assumptions

- **Synthetic Data Only**: All committed fixtures (`data/fixtures/market_features_*.json`) are deterministically generated synthetic 6-hour equity micro-alpha bars (`CC0-1.0-Synthetic`, `INTERNAL_SYNTHETIC_SANDBOX`).
- **Baseline Model Family**: The MVP reference trainer uses deterministic standardized Ridge (L2) linear regression implemented in pure Python to eliminate nondeterministic GPU/C-extension variance and guarantee byte-identical reproducibility across machines.
- **Transaction Cost Model**: Evaluation deducts a fixed configurable transaction cost (`5.0 bps` per unit position change). Market impact, queue position, partial fills, borrow costs, and latency slippage are **not** modeled.
- **No Live Performance Inference**: All reported Sharpe ratios, hit rates, IC, and drawdowns come from synthetic historical fixtures and **never** imply live trading performance.

## 3. Explicit List of Unsupported Features (Out of Scope for MVP)

1. **Real-Capital Live Order Execution**: Permanently blocked in MVP (`allow_live_execution_in_mvp = false`, `order_execution_enabled = false`). Real-capital deployment requires separate legal/regulatory, broker, security, and risk approval.
2. **Automatic Live Model Promotion**: Explicitly forbidden by configuration invariant (`allow_automatic_live_promotion = false`).
3. **Blind Automatic Retraining on Recent Returns**: Explicitly forbidden by configuration invariant (`allow_blind_recent_return_retraining = false`) and lineage span/policy enforcement.
4. **Live External Infrastructure Backends**: PostgreSQL (`PostgresRegistryStubAdapter`), S3 (`S3ArtifactStoreStubAdapter`), and MLflow (`MLflowTrackingStubAdapter`) are isolated behind clean adapter boundaries and are not connected in local sandbox mode.
5. **Distributed GPU / Multi-Node Training Scheduler**: Training executes deterministically in a single process; container/Kubernetes orchestration is deferred until asset class, strategy horizon, and compute constraints are confirmed.
6. **Hardware Security Module (HSM) / KMS Signing**: Local sandbox uses HMAC-SHA256 keyed via environment variable (`TRADING_MLOPS_SIGNING_KEY`); asymmetric KMS/HSM signatures (Ed25519/RSA-PSS) are recommended before production paper/sandbox rollout.
7. **Authentication / SSO Identity Provider**: `Actor` objects model authenticated principals; production deployment must derive `Actor` strictly from verified SSO/OIDC/mTLS tokens rather than caller-supplied fields.
