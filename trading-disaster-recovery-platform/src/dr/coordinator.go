package dr

import (
	"fmt"
	"time"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/fencing"
	"trading-dr-platform/src/reconcile"
	"trading-dr-platform/src/replay"
	"trading-dr-platform/src/storage"
)

// RecoveryRequest specifies inputs for a controlled node recovery or failover.
type RecoveryRequest struct {
	AccountID          string
	BackupID           string
	WALPath            string
	CrashRecovery      bool
	AcquirePrimary     bool
	PreFailureLastTime time.Time
	NowUTC             time.Time
}

// RecoveryResult captures the full outcome, gate status, and measured RPO/RTO of a recovery run.
type RecoveryResult struct {
	NodeID               string                          `json:"node_id"`
	Lease                *domain.Lease                   `json:"lease,omitempty"`
	BackupManifest       *storage.BackupManifest         `json:"backup_manifest,omitempty"`
	ReplayReport         *replay.ReplayReport            `json:"replay_report,omitempty"`
	MarkedUnknownOrders  []string                        `json:"marked_unknown_orders,omitempty"`
	ReconciliationReport *reconcile.ReconciliationReport `json:"reconciliation_report,omitempty"`
	GateStatus           domain.ExecutionGateStatus      `json:"gate_status"`
	MeasuredRPOMS        int64                           `json:"measured_rpo_ms"`
	MeasuredRTOMS        float64                         `json:"measured_rto_ms"`
	Metrics              domain.HealthMetrics            `json:"metrics"`
	BlockingError        string                          `json:"blocking_error,omitempty"`
}

// Node orchestrates fencing, WAL persistence, checkpoint/backup restore, replay,
// freshness checks, broker reconciliation, and fail-closed order gating.
type Node struct {
	NodeID             string
	Policy             domain.DRPolicy
	Engine             *replay.Engine
	WAL                *storage.WAL
	BackupMgr          *storage.BackupManager
	Fence              fencing.Coordinator
	Broker             reconcile.BrokerAdapter
	Reconciler         *reconcile.Reconciler
	ActiveLease        *domain.Lease
	LastReconciliation *reconcile.ReconciliationReport
}

// NewNode constructs a DR-managed trading node with execution disabled until fenced and reconciled.
func NewNode(
	nodeID string,
	policy domain.DRPolicy,
	wal *storage.WAL,
	backupMgr *storage.BackupManager,
	fence fencing.Coordinator,
	broker reconcile.BrokerAdapter,
) *Node {
	return &Node{
		NodeID:     nodeID,
		Policy:     policy,
		Engine:     replay.NewEngine(),
		WAL:        wal,
		BackupMgr:  backupMgr,
		Fence:      fence,
		Broker:     broker,
		Reconciler: reconcile.NewReconciler(),
	}
}

// AcquirePrimary acquires an exclusive primary fencing lease with a monotonic epoch.
func (n *Node) AcquirePrimary(now time.Time) (domain.Lease, error) {
	ttl := time.Duration(n.Policy.Fencing.LeaseTTLMS) * time.Millisecond
	lease, err := n.Fence.Acquire(n.NodeID, now, ttl)
	if err != nil {
		n.Engine.IncrementMetric(func(m *domain.HealthMetrics) {
			m.FencingRejections++
		})
		return domain.Lease{}, err
	}
	n.ActiveLease = &lease
	n.Engine.SetFencingEpoch(lease.Epoch)
	return lease, nil
}

// PersistAndApply validates the active primary fence, appends evt to the WAL, and applies it to local state.
func (n *Node) PersistAndApply(evt domain.TradingEvent, now time.Time) error {
	if n.ActiveLease == nil {
		return domain.NewError(domain.ErrFencingViolation, "dr", "node holds no active primary lease", true, false, nil, nil)
	}
	if err := n.Fence.ValidateFence(n.NodeID, n.ActiveLease.Epoch, now); err != nil {
		n.Engine.IncrementMetric(func(m *domain.HealthMetrics) {
			m.FencingRejections++
		})
		return err
	}
	evt.FencingEpoch = n.ActiveLease.Epoch
	rec, err := n.WAL.Append(evt, now)
	if err != nil {
		return err
	}
	_, _, err = n.Engine.ApplyEvent(evt, rec.Hash)
	return err
}

// CreateCheckpointBackup snapshots OMS+Ledger+Market state and writes a SHA-256 verified backup archive.
func (n *Node) CreateCheckpointBackup(backupID, checkpointID string, now time.Time) (storage.BackupManifest, error) {
	cp := n.Engine.Snapshot(checkpointID, now)
	return n.BackupMgr.CreateBackup(backupID, cp)
}

// CheckFreshness verifies checkpoint snapshot age and market-data feed freshness against policy limits.
func (n *Node) CheckFreshness(now time.Time) (bool, []string) {
	nowUTC := now.UTC()
	reasons := make([]string, 0)

	snapTS := n.Engine.LastSnapshotUTC()
	if !snapTS.IsZero() && n.Policy.Freshness.MaxSnapshotAgeMS > 0 {
		ageMS := nowUTC.Sub(snapTS).Milliseconds()
		if ageMS > n.Policy.Freshness.MaxSnapshotAgeMS {
			reasons = append(reasons, fmt.Sprintf("stale_checkpoint_snapshot: age=%dms > max=%dms", ageMS, n.Policy.Freshness.MaxSnapshotAgeMS))
		}
	}

	mktSnaps := n.Engine.MarketSnapshots()
	if len(mktSnaps) == 0 && n.Policy.Execution.RequireFreshSnapshot {
		reasons = append(reasons, "missing_market_data_snapshot")
	} else if len(mktSnaps) > 0 && n.Policy.Freshness.MaxMarketFeedAgeMS > 0 {
		var newest time.Time
		for _, ms := range mktSnaps {
			if ms.AvailableTimeUTC.After(newest) {
				newest = ms.AvailableTimeUTC
			}
		}
		ageMS := nowUTC.Sub(newest).Milliseconds()
		if ageMS > n.Policy.Freshness.MaxMarketFeedAgeMS {
			reasons = append(reasons, fmt.Sprintf("stale_market_feed: age=%dms > max=%dms", ageMS, n.Policy.Freshness.MaxMarketFeedAgeMS))
		}
	}

	if len(reasons) > 0 {
		n.Engine.IncrementMetric(func(m *domain.HealthMetrics) {
			m.StaleDataBlocks++
		})
		return false, reasons
	}
	return true, nil
}

// EvaluateExecutionGate evaluates all fail-closed safety invariants before permitting order submission.
func (n *Node) EvaluateExecutionGate(now time.Time) domain.ExecutionGateStatus {
	nowUTC := now.UTC()
	status := domain.ExecutionGateStatus{
		Allowed:      false,
		Mode:         "DISABLED",
		CheckedAtUTC: nowUTC,
	}

	if !n.Policy.Execution.PaperSandboxAllowed {
		status.BlockingReasons = append(status.BlockingReasons, "execution_disabled_by_policy")
	}

	// 1. Fencing check
	if n.ActiveLease == nil {
		status.BlockingReasons = append(status.BlockingReasons, "no_active_fencing_lease")
	} else {
		status.FencingEpoch = n.ActiveLease.Epoch
		if err := n.Fence.ValidateFence(n.NodeID, n.ActiveLease.Epoch, nowUTC); err != nil {
			n.Engine.IncrementMetric(func(m *domain.HealthMetrics) {
				m.FencingRejections++
			})
			status.BlockingReasons = append(status.BlockingReasons, fmt.Sprintf("fence_invalid: %v", err))
		} else {
			status.FenceValid = true
		}
	}

	// 2. Freshness check
	freshOK, freshReasons := n.CheckFreshness(nowUTC)
	status.FreshnessPassed = freshOK
	if !freshOK {
		status.BlockingReasons = append(status.BlockingReasons, freshReasons...)
	}

	// 3. Reconciliation check
	if n.LastReconciliation != nil && n.LastReconciliation.Passed {
		status.ReconciliationPassed = true
	} else if n.Policy.Execution.RequireReconciliation {
		status.BlockingReasons = append(status.BlockingReasons, "broker_reconciliation_not_passed")
	}

	// 4. Uncertain orders check
	if uncertain := n.Engine.OMS().UncertainOrders(); len(uncertain) > 0 && n.Policy.Execution.FailClosedOnUnknownOrder {
		status.BlockingReasons = append(status.BlockingReasons, fmt.Sprintf("uncertain_orders_present: %d", len(uncertain)))
	}

	if len(status.BlockingReasons) == 0 && n.Policy.Execution.PaperSandboxAllowed && !n.Policy.Execution.LiveExecutionAllowed {
		status.Allowed = true
		status.Mode = "PAPER_SANDBOX"
	}
	return status
}

// RecoverAndFailover executes the deterministic DR recovery pipeline:
// fence -> verify & restore backup -> verify & replay WAL -> mark in-flight unknown ->
// verify freshness -> reconcile broker -> evaluate fail-closed execution gate.
func (n *Node) RecoverAndFailover(req RecoveryRequest) (RecoveryResult, error) {
	start := time.Now()
	nowUTC := req.NowUTC.UTC()
	res := RecoveryResult{
		NodeID: n.NodeID,
	}

	// 1. Acquire or verify primary fencing lease.
	if req.AcquirePrimary {
		lease, err := n.AcquirePrimary(nowUTC)
		if err != nil {
			res.BlockingError = err.Error()
			res.GateStatus = n.EvaluateExecutionGate(nowUTC)
			res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
			res.Metrics = n.Engine.Metrics()
			return res, err
		}
		res.Lease = &lease
	} else if n.ActiveLease != nil {
		if err := n.Fence.ValidateFence(n.NodeID, n.ActiveLease.Epoch, nowUTC); err != nil {
			n.Engine.IncrementMetric(func(m *domain.HealthMetrics) {
				m.FencingRejections++
			})
			res.BlockingError = err.Error()
			res.GateStatus = n.EvaluateExecutionGate(nowUTC)
			res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
			res.Metrics = n.Engine.Metrics()
			return res, err
		}
		cp := *n.ActiveLease
		res.Lease = &cp
	} else {
		err := domain.NewError(domain.ErrFencingViolation, "dr", "cannot recover without primary fencing lease", true, false, nil, nil)
		res.BlockingError = err.Error()
		res.GateStatus = n.EvaluateExecutionGate(nowUTC)
		res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
		res.Metrics = n.Engine.Metrics()
		return res, err
	}

	// 2. Restore verified backup checkpoint if requested.
	if req.BackupID != "" {
		cpState, manifest, err := n.BackupMgr.RestoreBackup(req.BackupID, n.Policy.Backup.VerifySHA256OnRestore)
		if err != nil {
			n.Engine.IncrementMetric(func(m *domain.HealthMetrics) {
				m.CorruptionDetections++
			})
			res.BlockingError = err.Error()
			res.GateStatus = n.EvaluateExecutionGate(nowUTC)
			res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
			res.Metrics = n.Engine.Metrics()
			return res, err
		}
		if err := n.Engine.RestoreFromCheckpoint(cpState); err != nil {
			res.BlockingError = err.Error()
			res.GateStatus = n.EvaluateExecutionGate(nowUTC)
			res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
			res.Metrics = n.Engine.Metrics()
			return res, err
		}
		res.BackupManifest = &manifest
	}

	// 3. Verify and replay WAL tail.
	walPath := req.WALPath
	if walPath == "" && n.WAL != nil {
		walPath = n.WAL.Path()
	}
	if walPath != "" {
		records, err := storage.ReadWALFile(walPath, n.Policy.Backup.VerifyWALHashChain)
		if err != nil {
			n.Engine.IncrementMetric(func(m *domain.HealthMetrics) {
				m.CorruptionDetections++
			})
			res.BlockingError = err.Error()
			res.GateStatus = n.EvaluateExecutionGate(nowUTC)
			res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
			res.Metrics = n.Engine.Metrics()
			return res, err
		}
		rep, err := n.Engine.ReplayWAL(records, nowUTC)
		if err != nil {
			res.BlockingError = err.Error()
			res.GateStatus = n.EvaluateExecutionGate(nowUTC)
			res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
			res.Metrics = n.Engine.Metrics()
			return res, err
		}
		res.ReplayReport = &rep
	}

	// 4. Mark any in-flight PENDING_SUBMIT orders as UNKNOWN after crash so reconciliation resolves them.
	if req.CrashRecovery {
		res.MarkedUnknownOrders = n.Engine.MarkInFlightOrdersUnknown("process_crash_recovery", nowUTC)
	}

	// 5. Measure RPO against pre-failure committed event timestamp.
	if !req.PreFailureLastTime.IsZero() && !n.Engine.LastEventTimeUTC().IsZero() {
		rpo := req.PreFailureLastTime.UTC().Sub(n.Engine.LastEventTimeUTC()).Milliseconds()
		if rpo < 0 {
			rpo = 0
		}
		res.MeasuredRPOMS = rpo
	}

	// 6. Reconcile against authoritative broker state before enabling execution.
	recReport, recErr := n.Reconciler.Reconcile(req.AccountID, n.ActiveLease.Epoch, nowUTC, n.Engine, n.Broker, n.Policy)
	res.ReconciliationReport = &recReport
	n.LastReconciliation = &recReport

	// 7. Evaluate execution gate (checks fence, freshness, reconciliation, uncertain orders).
	res.GateStatus = n.EvaluateExecutionGate(nowUTC)
	res.MeasuredRTOMS = float64(time.Since(start).Microseconds()) / 1000.0
	res.Metrics = n.Engine.Metrics()

	if recErr != nil {
		res.BlockingError = recErr.Error()
		return res, recErr
	}
	if !res.GateStatus.Allowed {
		err := domain.NewError(domain.ErrExecutionBlocked, "dr",
			fmt.Sprintf("execution gate remains closed: %v", res.GateStatus.BlockingReasons),
			true, true, nil, map[string]any{"blocking_reasons": res.GateStatus.BlockingReasons})
		res.BlockingError = err.Error()
		return res, err
	}
	return res, nil
}

// SubmitOrder submits a new paper/sandbox order only if the fail-closed execution gate and fence pass.
func (n *Node) SubmitOrder(evt domain.TradingEvent, now time.Time) (string, error) {
	gate := n.EvaluateExecutionGate(now)
	if !gate.Allowed {
		return "", domain.NewError(domain.ErrExecutionBlocked, "dr",
			fmt.Sprintf("order %s blocked by fail-closed execution gate: %v", evt.ClientOrderID, gate.BlockingReasons),
			true, false, nil, map[string]any{"blocking_reasons": gate.BlockingReasons})
	}
	if err := n.PersistAndApply(evt, now); err != nil {
		return "", err
	}
	rec, _ := n.Engine.OMS().GetOrder(evt.ClientOrderID)
	brokerOrderID, err := n.Broker.SubmitOrder(rec, n.ActiveLease.Epoch)
	if err != nil {
		if domain.HasCode(err, domain.ErrSplitBrainDetected) || domain.HasCode(err, domain.ErrFencingViolation) {
			n.Engine.IncrementMetric(func(m *domain.HealthMetrics) { m.FencingRejections++ })
		}
		_ = n.Engine.OMS().MarkOrderUnknown(evt.ClientOrderID, "broker_submit_error", now)
		return "", err
	}
	_, _ = n.Engine.OMS().ApplyAck(evt.ClientOrderID, brokerOrderID, now)
	return brokerOrderID, nil
}
