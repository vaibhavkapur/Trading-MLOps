package replay

import (
	"fmt"
	"sync"
	"time"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/ledger"
	"trading-dr-platform/src/oms"
	"trading-dr-platform/src/storage"
)

// ReplayReport summarizes deterministic WAL replay results and provenance.
type ReplayReport struct {
	StartingSequence  uint64    `json:"starting_sequence"`
	EndingSequence    uint64    `json:"ending_sequence"`
	EventsScanned     int       `json:"events_scanned"`
	EventsApplied     int       `json:"events_applied"`
	DuplicatesIgnored int       `json:"duplicates_ignored"`
	LastWALHash       string    `json:"last_wal_hash"`
	DurationMS        float64   `json:"duration_ms"`
	RecoveredAtUTC    time.Time `json:"recovered_at_utc"`
}

// Engine executes deterministic, idempotent event processing and checkpoint/WAL recovery
// across the OMS, Ledger, and MarketSnapshot stores.
type Engine struct {
	mu                sync.RWMutex
	ledger            *ledger.Ledger
	oms               *oms.OMS
	processedEventIDs map[string]bool
	marketSnapshots   map[string]domain.MarketSnapshot
	lastSequence      uint64
	lastEventTimeUTC  time.Time
	lastWALHash       string
	lastSnapshotUTC   time.Time
	fencingEpoch      uint64
	metrics           domain.HealthMetrics
}

// NewEngine creates a clean deterministic recovery Engine.
func NewEngine() *Engine {
	return &Engine{
		ledger:            ledger.New(),
		oms:               oms.New(),
		processedEventIDs: make(map[string]bool),
		marketSnapshots:   make(map[string]domain.MarketSnapshot),
		lastWALHash:       storage.GenesisHash,
	}
}

// Ledger returns the engine's Ledger instance.
func (e *Engine) Ledger() *ledger.Ledger { return e.ledger }

// OMS returns the engine's OMS instance.
func (e *Engine) OMS() *oms.OMS { return e.oms }

// Metrics returns a copy of current operational health metrics.
func (e *Engine) Metrics() domain.HealthMetrics {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.metrics
}

// LastSequence returns the highest event sequence applied.
func (e *Engine) LastSequence() uint64 {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.lastSequence
}

// LastEventTimeUTC returns the UTC timestamp of the latest event applied.
func (e *Engine) LastEventTimeUTC() time.Time {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.lastEventTimeUTC
}

// LastSnapshotUTC returns the creation timestamp of the latest loaded/created checkpoint.
func (e *Engine) LastSnapshotUTC() time.Time {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.lastSnapshotUTC
}

// MarketSnapshots returns a copy of the latest market snapshots keyed by symbol.
func (e *Engine) MarketSnapshots() map[string]domain.MarketSnapshot {
	e.mu.RLock()
	defer e.mu.RUnlock()
	out := make(map[string]domain.MarketSnapshot, len(e.marketSnapshots))
	for k, v := range e.marketSnapshots {
		out[k] = v
	}
	return out
}

// SetFencingEpoch updates the engine's active fencing epoch.
func (e *Engine) SetFencingEpoch(epoch uint64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.fencingEpoch = epoch
}

// IncrementMetric allows coordinator components to record safety/control metrics.
func (e *Engine) IncrementMetric(fn func(m *domain.HealthMetrics)) {
	e.mu.Lock()
	defer e.mu.Unlock()
	fn(&e.metrics)
}

// ApplyEvent validates and idempotently applies a TradingEvent to OMS, Ledger, and MarketSnapshots.
// Returns (applied, duplicate, error).
func (e *Engine) ApplyEvent(evt domain.TradingEvent, walHash string) (bool, bool, error) {
	if err := evt.Validate(); err != nil {
		return false, false, err
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	if e.processedEventIDs[evt.EventID] {
		e.metrics.DuplicateEventsIgnored++
		return false, true, nil
	}

	var duplicate bool
	var err error

	switch evt.EventType {
	case domain.EventCashDeposit:
		duplicate, err = e.ledger.ApplyCashDeposit(evt.EventID, evt.AccountID, evt.Currency, evt.AmountMinor, evt.EventTimeUTC)
	case domain.EventCashWithdrawal:
		duplicate, err = e.ledger.ApplyCashWithdrawal(evt.EventID, evt.AccountID, evt.Currency, evt.AmountMinor, evt.EventTimeUTC)
	case domain.EventMarketSnapshot:
		e.marketSnapshots[evt.Symbol] = domain.MarketSnapshot{
			Symbol:           evt.Symbol,
			PriceMinor:       evt.PriceMinor,
			Currency:         evt.Currency,
			EventTimeUTC:     evt.EventTimeUTC.UTC(),
			AvailableTimeUTC: evt.AvailableTimeUTC.UTC(),
		}
	case domain.EventOrderIntent:
		duplicate, err = e.oms.RecordIntent(evt)
	case domain.EventOrderAck:
		duplicate, err = e.oms.ApplyAck(evt.ClientOrderID, evt.BrokerOrderID, evt.EventTimeUTC)
	case domain.EventOrderFill:
		fill := domain.FillRecord{
			FillID:        evt.FillID,
			ClientOrderID: evt.ClientOrderID,
			BrokerOrderID: evt.BrokerOrderID,
			AccountID:     evt.AccountID,
			Symbol:        evt.Symbol,
			Side:          evt.Side,
			Quantity:      evt.Quantity,
			PriceMinor:    evt.PriceMinor,
			FeeMinor:      evt.FeeMinor,
			Currency:      evt.Currency,
			ExecutedAtUTC: evt.EventTimeUTC.UTC(),
		}
		var dupOMS, dupLedger bool
		dupOMS, err = e.oms.ApplyFill(fill)
		if err != nil {
			return false, false, err
		}
		dupLedger, err = e.ledger.ApplyFill(evt.EventID, fill)
		if err != nil {
			return false, false, err
		}
		duplicate = dupOMS || dupLedger
		if duplicate {
			e.metrics.DuplicateFillsIgnored++
		}
	case domain.EventOrderCancel:
		duplicate, err = e.oms.ApplyCancel(evt.ClientOrderID, evt.Reason, evt.EventTimeUTC)
	case domain.EventOrderReject:
		duplicate, err = e.oms.ApplyReject(evt.ClientOrderID, evt.Reason, evt.EventTimeUTC)
	case domain.EventOrderMarkUnk:
		err = e.oms.MarkOrderUnknown(evt.ClientOrderID, evt.Reason, evt.EventTimeUTC)
		if err == nil {
			e.metrics.UncertainOrdersDetected++
		}
	default:
		return false, false, domain.NewError(domain.ErrInvalidInput, "replay", fmt.Sprintf("unhandled event type %s", evt.EventType), true, false, nil, nil)
	}

	if err != nil {
		return false, false, err
	}

	e.processedEventIDs[evt.EventID] = true
	if evt.Sequence > e.lastSequence {
		e.lastSequence = evt.Sequence
	}
	if evt.EventTimeUTC.After(e.lastEventTimeUTC) {
		e.lastEventTimeUTC = evt.EventTimeUTC.UTC()
	}
	if walHash != "" {
		e.lastWALHash = walHash
	}

	if duplicate {
		e.metrics.DuplicateEventsIgnored++
		return false, true, nil
	}
	e.metrics.EventsApplied++
	return true, false, nil
}

// Snapshot builds a deterministic CheckpointState for backup persistence.
func (e *Engine) Snapshot(checkpointID string, now time.Time) domain.CheckpointState {
	e.mu.Lock()
	defer e.mu.Unlock()

	proc := make(map[string]bool, len(e.processedEventIDs))
	for k, v := range e.processedEventIDs {
		proc[k] = v
	}
	mkt := make(map[string]domain.MarketSnapshot, len(e.marketSnapshots))
	for k, v := range e.marketSnapshots {
		mkt[k] = v
	}
	e.lastSnapshotUTC = now.UTC()
	e.metrics.CheckpointsWritten++

	return domain.CheckpointState{
		SchemaVersion:     "1.0.0",
		CheckpointID:      checkpointID,
		CreatedAtUTC:      now.UTC(),
		LastSequence:      e.lastSequence,
		LastEventTimeUTC:  e.lastEventTimeUTC.UTC(),
		LastWALHash:       e.lastWALHash,
		FencingEpoch:      e.fencingEpoch,
		ProcessedEventIDs: proc,
		MarketSnapshots:   mkt,
		Ledger:            e.ledger.Snapshot(),
		OMS:               e.oms.Snapshot(),
	}
}

// RestoreFromCheckpoint restores full OMS, Ledger, MarketSnapshot, and dedup state from a verified CheckpointState.
func (e *Engine) RestoreFromCheckpoint(cp domain.CheckpointState) error {
	e.mu.Lock()
	defer e.mu.Unlock()

	if err := e.ledger.Restore(cp.Ledger); err != nil {
		return err
	}
	if err := e.oms.Restore(cp.OMS); err != nil {
		return err
	}
	e.processedEventIDs = make(map[string]bool, len(cp.ProcessedEventIDs))
	for k, v := range cp.ProcessedEventIDs {
		e.processedEventIDs[k] = v
	}
	e.marketSnapshots = make(map[string]domain.MarketSnapshot, len(cp.MarketSnapshots))
	for k, v := range cp.MarketSnapshots {
		e.marketSnapshots[k] = v
	}
	e.lastSequence = cp.LastSequence
	e.lastEventTimeUTC = cp.LastEventTimeUTC.UTC()
	e.lastWALHash = cp.LastWALHash
	e.lastSnapshotUTC = cp.CreatedAtUTC.UTC()
	e.fencingEpoch = cp.FencingEpoch
	e.metrics.BackupsRestored++
	return nil
}

// ReplayWAL idempotently replays WAL records on top of the current engine state and verifies ledger invariants.
func (e *Engine) ReplayWAL(records []storage.WALRecord, now time.Time) (ReplayReport, error) {
	start := time.Now()
	report := ReplayReport{
		StartingSequence: e.LastSequence(),
		EventsScanned:    len(records),
		RecoveredAtUTC:   now.UTC(),
	}

	for _, rec := range records {
		applied, dup, err := e.ApplyEvent(rec.Event, rec.Hash)
		if err != nil {
			return report, err
		}
		if dup {
			report.DuplicatesIgnored++
		} else if applied {
			report.EventsApplied++
		}
		report.LastWALHash = rec.Hash
	}

	if err := e.ledger.VerifyJournalInvariant(); err != nil {
		return report, err
	}
	report.EndingSequence = e.LastSequence()
	report.DurationMS = float64(time.Since(start).Microseconds()) / 1000.0
	return report, nil
}

// MarkInFlightOrdersUnknown flags all PENDING_SUBMIT orders as UNKNOWN after an unclean crash.
func (e *Engine) MarkInFlightOrdersUnknown(reason string, now time.Time) []string {
	marked := e.oms.MarkPendingAsUnknown(reason, now)
	if len(marked) > 0 {
		e.mu.Lock()
		e.metrics.UncertainOrdersDetected += uint64(len(marked))
		e.mu.Unlock()
	}
	return marked
}
