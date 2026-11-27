# API, Domain Contracts, and Schemas

## Design Conventions

- **Stable Identifiers:** `event_id`, `client_order_id`, `broker_order_id`, `fill_id`, `idempotency_key`, `account_id`, `checkpoint_id`, `backup_id`.
- **Units & Currency:** All monetary amounts (`amount_minor`, `price_minor`, `limit_price_minor`, `fee_minor`, `cash_delta_minor`) are `int64` in ISO-4217 minor units (e.g., `USD` cents where `100` = \$1.00) paired with an explicit 3-letter `currency` field. No floating-point currency math is used.
- **Timestamps:** All timestamps are UTC RFC3339Nano. Every `TradingEvent` carries both `event_time_utc` (when the domain event occurred) and `available_time_utc` (when it became observable), enforcing `available_time_utc >= event_time_utc`.
- **Versioned Configuration:** `config/dr_policy.v1.json` and `config/failure_modes.v1.json` include explicit `schema_version` (`1.0.0`).

## Canonical Event Envelope (`TradingEvent`)

```json
{
  "event_id": "evt-0005",
  "sequence": 5,
  "event_type": "ORDER_FILL",
  "event_time_utc": "2026-01-15T14:30:02.120Z",
  "available_time_utc": "2026-01-15T14:30:02.125Z",
  "account_id": "ACCT-PAPER-01",
  "client_order_id": "ORD-1001",
  "broker_order_id": "BRK-9001",
  "fill_id": "FILL-5001",
  "symbol": "AAPL",
  "side": "BUY",
  "quantity": 100,
  "price_minor": 18500,
  "fee_minor": 100,
  "currency": "USD",
  "idempotency_key": "fill-5001",
  "source": "broker"
}
```

Supported `event_type` values: `CASH_DEPOSIT`, `CASH_WITHDRAWAL`, `MARKET_SNAPSHOT`, `ORDER_INTENT`, `ORDER_ACK`, `ORDER_FILL`, `ORDER_CANCEL`, `ORDER_REJECT`, `ORDER_MARK_UNKNOWN`.

## OMS Order Lifecycle

`PENDING_SUBMIT` -> `UNKNOWN` (on crash recovery) -> `ACKED` -> `PARTIALLY_FILLED` -> `FILLED` | `CANCELED` | `REJECTED`.

- Terminal states (`FILLED`, `CANCELED`, `REJECTED`) cannot transition to open states or accept fills.
- Duplicate `idempotency_key` with matching parameters is ignored idempotently; conflicting parameters raise `DUPLICATE_EVENT`.
- Duplicate `fill_id` is ignored idempotently in both OMS and Ledger.

## Structured Error Codes (`domain.DRError`)

| Error Code | Fail-Closed | Meaning |
|---|---:|---|
| `INVALID_INPUT` | true | Malformed event, timestamp ordering violation, or invalid currency/quantity |
| `POLICY_VIOLATION` | true | Unsafe policy (e.g., `live_execution_allowed=true` or disabled checksum verification) |
| `BACKUP_CORRUPTED` | true | Backup SHA-256 digest, byte size, gzip stream, or JSON payload corrupted |
| `WAL_CORRUPTED` | true | WAL record index gap, `prev_hash` break, or payload digest mismatch |
| `DUPLICATE_EVENT` | true | Conflicting parameters submitted under an already-bound `idempotency_key` |
| `FENCING_VIOLATION` | true | Missing lease, expired lease, active lease held by another node, or network partition |
| `SPLIT_BRAIN_DETECTED` | true | Caller presented a stale fencing epoch `< highest_epoch` |
| `STALE_SNAPSHOT` | true | Checkpoint, market feed, or broker snapshot age exceeds configured freshness limit |
| `BROKER_UNAVAILABLE` | true | Broker API timeout or connectivity outage |
| `UNKNOWN_EXTERNAL_STATE` | true | External broker balances/orders cannot be verified during recovery |
| `RECONCILIATION_MISMATCH` | true | Local ledger cash/positions or broker orders disagree outside policy tolerance |
| `EXECUTION_BLOCKED` | true | Order submission rejected because the fail-closed execution gate is closed |
| `ORDER_TRANSITION_INVALID` | true | Illegal OMS state transition or attempted overfill |

## Pluggable Adapter Interfaces

1. **`storage.ObjectStore`**: `Put(key, data)`, `Get(key)`, `List(prefix)` — implemented locally by `LocalObjectStore` (atomic temp+fsync+rename); ready for S3/MinIO/GCS adapters.
2. **`fencing.Coordinator`**: `Acquire`, `Renew`, `Release`, `ValidateFence`, `CurrentLease` — implemented locally by `FileCoordinator`; ready for etcd/Consul/PostgreSQL advisory-lock adapters.
3. **`reconcile.BrokerAdapter`**: `GetAccountSnapshot`, `GetOrderByClientID`, `SubmitOrder` (all accepting monotonic `fencingEpoch`) — implemented locally by `SimulatedBroker` with deterministic fault injection.

## CLI Interface (`drctl`)

- `drctl demo [--policy ...] [--fixtures ...] [--broker ...]`
- `drctl drills [--policy ...] [--failure-modes ...] [--fixtures ...] [--broker ...] [--out-json ...] [--out-md ...]`
- `drctl verify-wal --wal <path>`
- `drctl verify-backup --store <dir> --backup-id <id>`
