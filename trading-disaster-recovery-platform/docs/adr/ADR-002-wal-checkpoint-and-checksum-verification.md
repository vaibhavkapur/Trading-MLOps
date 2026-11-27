# ADR-002: Hash-Chained WAL and SHA-256 Verified Checkpoint Backups

- **Status:** Accepted (MVP)
- **Date:** 2026-11-27

## Context
Restoring a trading OMS and ledger from silently corrupted backups or truncated/tampered WAL logs can cause phantom positions, corrupted cash balances, or lost open-order tracking.

## Decision
1. Every event persisted to the WAL (`src/storage/wal.go`) is wrapped in a `WALRecord` containing a sequential `record_index`, `prev_hash`, and `hash = SHA-256(record_index | prev_hash | canonical_event_json)`, followed by `fsync`.
2. Every checkpoint backup (`src/storage/backup.go`) serializes full deterministic state (OMS, Ledger, MarketSnapshots, deduplication registry), compresses with gzip, writes payload and manifest atomically via temp-file + `fsync` + rename, and records `payload_sha256` and `payload_bytes`.
3. Restore verifies both backup SHA-256 and the full WAL hash chain before replaying state; any mismatch returns `BACKUP_CORRUPTED` or `WAL_CORRUPTED` and aborts recovery fail-closed.

## Consequences
- Guarantees bit-rot, truncation, and tampering are detected prior to state reconstruction.
- Slightly increases write latency due to per-record hashing and `fsync` (acceptable for order-level durability; high-frequency market ticks should be archived separately in production).
