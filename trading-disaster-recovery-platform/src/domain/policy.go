package domain

import (
	"encoding/json"
	"fmt"
	"os"
)

// ExecutionPolicy controls whether execution can be enabled and enforces fail-closed rules.
type ExecutionPolicy struct {
	LiveExecutionAllowed     bool `json:"live_execution_allowed"`
	PaperSandboxAllowed      bool `json:"paper_sandbox_allowed"`
	RequireReconciliation    bool `json:"require_reconciliation_pass"`
	RequireValidFence        bool `json:"require_valid_fence"`
	RequireFreshSnapshot     bool `json:"require_fresh_snapshot"`
	FailClosedOnUnknownOrder bool `json:"fail_closed_on_unknown_order"`
	FailClosedOnBrokerOutage bool `json:"fail_closed_on_broker_outage"`
}

// FencingPolicy defines lease TTL and monotonic epoch enforcement.
type FencingPolicy struct {
	LeaseTTLMS            int64 `json:"lease_ttl_ms"`
	ClockSkewToleranceMS  int64 `json:"clock_skew_tolerance_ms"`
	RequireMonotonicEpoch bool  `json:"require_monotonic_epoch"`
}

// FreshnessPolicy bounds acceptable snapshot and feed ages.
type FreshnessPolicy struct {
	MaxSnapshotAgeMS       int64 `json:"max_snapshot_age_ms"`
	MaxMarketFeedAgeMS     int64 `json:"max_market_feed_age_ms"`
	MaxBrokerSnapshotAgeMS int64 `json:"max_broker_snapshot_age_ms"`
}

// BackupPolicy governs integrity checks and checkpoint cadence.
type BackupPolicy struct {
	VerifySHA256OnRestore    bool `json:"verify_sha256_on_restore"`
	VerifyWALHashChain       bool `json:"verify_wal_hash_chain"`
	CheckpointIntervalEvents int  `json:"checkpoint_interval_events"`
}

// ReconciliationPolicy defines tolerances for cash and position reconciliation.
type ReconciliationPolicy struct {
	MaxCashToleranceMinor        int64 `json:"max_cash_tolerance_minor"`
	MaxPositionToleranceQty      int64 `json:"max_position_tolerance_qty"`
	AbortOnUnexpectedBrokerOrder bool  `json:"abort_on_unexpected_broker_order"`
}

// ComponentObjective defines proposed RPO and RTO per platform component.
type ComponentObjective struct {
	Component     string `json:"component"`
	ProposedRPOMS int64  `json:"proposed_rpo_ms"`
	ProposedRTOMS int64  `json:"proposed_rto_ms"`
	Notes         string `json:"notes"`
}

// DRPolicy is the versioned operational configuration loaded from config/dr_policy.v1.json.
type DRPolicy struct {
	SchemaVersion  string               `json:"schema_version"`
	PolicyID       string               `json:"policy_id"`
	Environment    string               `json:"environment"`
	Execution      ExecutionPolicy      `json:"execution_policy"`
	Fencing        FencingPolicy        `json:"fencing"`
	Freshness      FreshnessPolicy      `json:"freshness"`
	Backup         BackupPolicy         `json:"backup"`
	Reconciliation ReconciliationPolicy `json:"reconciliation"`
	Objectives     []ComponentObjective `json:"objectives_by_component"`
}

// Validate verifies required fields and safety invariants in DRPolicy.
func (p DRPolicy) Validate() error {
	if p.SchemaVersion == "" || p.PolicyID == "" {
		return NewError(ErrPolicyViolation, "policy", "schema_version and policy_id are required", true, false, nil, nil)
	}
	if p.Execution.LiveExecutionAllowed {
		return NewError(ErrPolicyViolation, "policy", "live_execution_allowed must remain false in MVP/sandbox policy", true, false, nil, nil)
	}
	if !p.Fencing.RequireMonotonicEpoch || p.Fencing.LeaseTTLMS <= 0 {
		return NewError(ErrPolicyViolation, "policy", "fencing requires monotonic epoch and positive lease_ttl_ms", true, false, nil, nil)
	}
	if p.Freshness.MaxSnapshotAgeMS <= 0 || p.Freshness.MaxMarketFeedAgeMS <= 0 {
		return NewError(ErrPolicyViolation, "policy", "freshness thresholds must be positive", true, false, nil, nil)
	}
	if !p.Backup.VerifySHA256OnRestore || !p.Backup.VerifyWALHashChain {
		return NewError(ErrPolicyViolation, "policy", "checksum and WAL hash-chain verification cannot be disabled", true, false, nil, nil)
	}
	if len(p.Objectives) == 0 {
		return NewError(ErrPolicyViolation, "policy", "at least one component recovery objective is required", true, false, nil, nil)
	}
	return nil
}

// ObjectiveFor returns the component objective if defined.
func (p DRPolicy) ObjectiveFor(component string) (ComponentObjective, bool) {
	for _, o := range p.Objectives {
		if o.Component == component {
			return o, true
		}
	}
	return ComponentObjective{}, false
}

// FailureMode describes an inventoried failure scenario and its expected recovery behavior.
type FailureMode struct {
	ID               string `json:"id"`
	ScenarioKey      string `json:"scenario_key"`
	Component        string `json:"component"`
	Severity         string `json:"severity"`
	Description      string `json:"description"`
	Detection        string `json:"detection"`
	ExpectedBehavior string `json:"expected_behavior"`
	FailClosed       bool   `json:"fail_closed"`
	ProposedRPOMS    int64  `json:"proposed_rpo_ms"`
	ProposedRTOMS    int64  `json:"proposed_rto_ms"`
}

// FailureModeInventory holds all registered failure modes.
type FailureModeInventory struct {
	SchemaVersion string        `json:"schema_version"`
	InventoryID   string        `json:"inventory_id"`
	FailureModes  []FailureMode `json:"failure_modes"`
}

// Validate checks inventory integrity and uniqueness of IDs and scenario keys.
func (inv FailureModeInventory) Validate() error {
	if inv.SchemaVersion == "" || inv.InventoryID == "" {
		return NewError(ErrInvalidInput, "policy", "inventory schema_version and inventory_id are required", true, false, nil, nil)
	}
	if len(inv.FailureModes) == 0 {
		return NewError(ErrInvalidInput, "policy", "failure_modes must not be empty", true, false, nil, nil)
	}
	seenID := make(map[string]bool)
	seenKey := make(map[string]bool)
	for _, fm := range inv.FailureModes {
		if fm.ID == "" || fm.ScenarioKey == "" || fm.Component == "" {
			return NewError(ErrInvalidInput, "policy", "failure mode missing id, scenario_key, or component", true, false, nil, nil)
		}
		if seenID[fm.ID] {
			return NewError(ErrInvalidInput, "policy", fmt.Sprintf("duplicate failure mode id %s", fm.ID), true, false, nil, nil)
		}
		if seenKey[fm.ScenarioKey] {
			return NewError(ErrInvalidInput, "policy", fmt.Sprintf("duplicate scenario_key %s", fm.ScenarioKey), true, false, nil, nil)
		}
		seenID[fm.ID] = true
		seenKey[fm.ScenarioKey] = true
	}
	return nil
}

// ByScenarioKey looks up a FailureMode by scenario key.
func (inv FailureModeInventory) ByScenarioKey(key string) (FailureMode, bool) {
	for _, fm := range inv.FailureModes {
		if fm.ScenarioKey == key {
			return fm, true
		}
	}
	return FailureMode{}, false
}

// LoadPolicy loads and validates a DRPolicy from a JSON file.
func LoadPolicy(path string) (DRPolicy, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return DRPolicy{}, NewError(ErrInvalidInput, "policy", "failed reading policy file", true, false, err, map[string]any{"path": path})
	}
	var p DRPolicy
	if err := json.Unmarshal(raw, &p); err != nil {
		return DRPolicy{}, NewError(ErrInvalidInput, "policy", "failed parsing policy JSON", true, false, err, map[string]any{"path": path})
	}
	if err := p.Validate(); err != nil {
		return DRPolicy{}, err
	}
	return p, nil
}

// LoadFailureModes loads and validates a FailureModeInventory from a JSON file.
func LoadFailureModes(path string) (FailureModeInventory, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return FailureModeInventory{}, NewError(ErrInvalidInput, "policy", "failed reading failure modes file", true, false, err, map[string]any{"path": path})
	}
	var inv FailureModeInventory
	if err := json.Unmarshal(raw, &inv); err != nil {
		return FailureModeInventory{}, NewError(ErrInvalidInput, "policy", "failed parsing failure modes JSON", true, false, err, map[string]any{"path": path})
	}
	if err := inv.Validate(); err != nil {
		return FailureModeInventory{}, err
	}
	return inv, nil
}
