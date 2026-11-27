# Security, Data Entitlements, and Model Governance Controls

## 1. Role-Based Access Control (RBAC) Matrix

| Role | Register Dataset/Features | Run Training | Run Validation Gate | Approve SHADOW/PAPER | Co-Approve LIVE_GATED | Promote / Deploy | Rollback | Audit / Monitor |
|---|---|---|---|---|---|---|---|---|
| `RESEARCHER` | Yes | Yes | No | No | No | No | No | Read |
| `VALIDATOR` | No | No | Yes | No | No | No | No | Read |
| `RISK_APPROVER` | No | No | Yes | Yes (not creator) | Yes (not creator) | No | Yes | Read |
| `COMPLIANCE_OFFICER` | No | No | No | No | Yes (not creator) | No | No | Read |
| `RELEASE_OPERATOR` | Bind Env Contracts | Optional | No | No | No | Yes | Yes | Read |
| `GOVERNANCE_AUDITOR` | No | No | Yes | No | No | No | No | Yes |

## 2. Separation of Duties

- `forbid_self_approval` is hard-enforced by `PlatformConfig` (cannot be disabled).
- Even if an account is assigned `RISK_APPROVER` or `COMPLIANCE_OFFICER`, approving an artifact where `approver.actor_id == artifact.created_by` raises `SELF_APPROVAL_FORBIDDEN` and writes a `BLOCKED` event to the hash-chained audit log.
- `LIVE_GATED` requires two distinct independent approvers covering both `RISK_APPROVER` and `COMPLIANCE_OFFICER`.

## 3. Credential and Secret Hygiene

- `config/platform_config.json` contains **zero secrets**—only the environment variable name (`TRADING_MLOPS_SIGNING_KEY`).
- Database DSNs (`TRADING_MLOPS_POSTGRES_DSN`) and object storage buckets (`TRADING_MLOPS_ARTIFACT_BUCKET`) are likewise read from environment variables outside source code.
- Unsafe serialization formats (`.pkl`, `.pickle`, `.joblib`, `.pt`, `.pth`, `.dill`, `.cloudpickle`) are explicitly rejected on load to eliminate arbitrary code execution via deserialization.

## 4. Data Entitlements and Provenance

- Every `DatasetSnapshot` requires non-empty `entitlement`, `license_name`, `content_sha256`, `schema_hash`, and `is_synthetic` flag.
- Synthetic fixtures committed in `data/fixtures/` are labeled `INTERNAL_SYNTHETIC_SANDBOX` / `CC0-1.0-Synthetic`.

## 5. Order Execution Kill-Switch and Real-Capital Gate

- `order_execution_enabled` defaults to `false` across configuration, deployments, rollbacks, and exported inference manifests.
- `SHADOW` permanently forbids execution.
- `LIVE_GATED` blocks `enable_order_execution=True` in MVP (`EXECUTION_DISABLED_GUARD`). Real-capital deployment is an external governance decision requiring legal/regulatory, broker, security, and risk sign-off.
