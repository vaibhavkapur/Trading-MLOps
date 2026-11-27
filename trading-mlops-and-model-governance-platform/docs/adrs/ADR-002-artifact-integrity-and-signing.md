# ADR-002: Safe JSON Artifact Bundles with SHA-256 and HMAC-SHA256 Signing

- **Status:** Accepted
- **Milestone:** M2

## Context
Python pickle/joblib/PyTorch checkpoints allow arbitrary code execution on load and are difficult to diff or verify deterministically. Undetected artifact tampering between training and deployment poses severe financial risk.

## Decision
1. Store model weights and evaluation manifests strictly in canonical sorted-key UTF-8 JSON (`safe_json_v1`) with deterministic SHA-256 digests per file and an aggregated `bundle_sha256`.
2. Sign `bundle_sha256` with HMAC-SHA256 using a key sourced exclusively from environment variable `TRADING_MLOPS_SIGNING_KEY`.
3. Reject path traversal and any `.pkl`/`.joblib`/`.pt` files present in a bundle directory, and re-verify checksums + signature at validation, promotion, monitoring, export, and rollback.

## Consequences
- Identical training specs produce byte-identical bundles and identical `bundle_sha256` across isolated environments.
- Before production rollout, HMAC-SHA256 should be upgraded to asymmetric KMS/HSM signatures so verification nodes do not hold signing keys.
