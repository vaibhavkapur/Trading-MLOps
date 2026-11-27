package oms

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"trading-dr-platform/src/domain"
)

// OMS tracks durable order intents, lifecycle state transitions, idempotency keys, and uncertain orders.
type OMS struct {
	mu              sync.RWMutex
	orders          map[string]*domain.OrderRecord // client_order_id -> record
	idempotencyKeys map[string]string              // idempotency_key -> client_order_id
	appliedFills    map[string]bool                // fill_id -> applied
	fills           []domain.FillRecord
}

// New creates an empty OMS.
func New() *OMS {
	return &OMS{
		orders:          make(map[string]*domain.OrderRecord),
		idempotencyKeys: make(map[string]string),
		appliedFills:    make(map[string]bool),
		fills:           make([]domain.FillRecord, 0),
	}
}

// RecordIntent registers a new order intent in PENDING_SUBMIT state.
// If the same idempotency_key or client_order_id already exists with identical intent parameters,
// it is treated as an idempotent duplicate (returns duplicate=true, nil).
// Conflicting parameters for an existing key return ErrDuplicateEvent.
func (o *OMS) RecordIntent(evt domain.TradingEvent) (bool, error) {
	o.mu.Lock()
	defer o.mu.Unlock()

	if existingID, ok := o.idempotencyKeys[evt.IdempotencyKey]; ok {
		existing := o.orders[existingID]
		if existing != nil && existing.ClientOrderID == evt.ClientOrderID &&
			existing.Symbol == evt.Symbol && existing.Side == evt.Side && existing.Quantity == evt.Quantity {
			return true, nil
		}
		return false, domain.NewError(domain.ErrDuplicateEvent, "oms",
			fmt.Sprintf("idempotency key %q already bound to conflicting order %s", evt.IdempotencyKey, existingID),
			true, false, nil, map[string]any{"idempotency_key": evt.IdempotencyKey, "existing_order": existingID})
	}

	if existing, ok := o.orders[evt.ClientOrderID]; ok {
		if existing.IdempotencyKey == evt.IdempotencyKey && existing.Symbol == evt.Symbol &&
			existing.Side == evt.Side && existing.Quantity == evt.Quantity {
			return true, nil
		}
		return false, domain.NewError(domain.ErrDuplicateEvent, "oms",
			fmt.Sprintf("client_order_id %q already exists with different parameters", evt.ClientOrderID),
			true, false, nil, map[string]any{"client_order_id": evt.ClientOrderID})
	}

	rec := &domain.OrderRecord{
		ClientOrderID:   evt.ClientOrderID,
		IdempotencyKey:  evt.IdempotencyKey,
		AccountID:       evt.AccountID,
		Symbol:          evt.Symbol,
		Side:            evt.Side,
		Quantity:        evt.Quantity,
		FilledQuantity:  0,
		LimitPriceMinor: evt.LimitPriceMinor,
		Currency:        evt.Currency,
		Status:          domain.OrderPendingSubmit,
		FencingEpoch:    evt.FencingEpoch,
		CreatedAtUTC:    evt.EventTimeUTC.UTC(),
		UpdatedAtUTC:    evt.EventTimeUTC.UTC(),
	}
	o.orders[evt.ClientOrderID] = rec
	o.idempotencyKeys[evt.IdempotencyKey] = evt.ClientOrderID
	return false, nil
}

// ApplyAck transitions a PENDING_SUBMIT or UNKNOWN order to ACKED.
func (o *OMS) ApplyAck(clientOrderID, brokerOrderID string, ts time.Time) (bool, error) {
	o.mu.Lock()
	defer o.mu.Unlock()

	rec, ok := o.orders[clientOrderID]
	if !ok {
		return false, domain.NewError(domain.ErrNotFound, "oms", fmt.Sprintf("order %s not found for ack", clientOrderID), true, false, nil, nil)
	}
	if rec.Status == domain.OrderAcked && rec.BrokerOrderID == brokerOrderID {
		return true, nil
	}
	if rec.Status == domain.OrderFilled || rec.Status == domain.OrderPartiallyFilled {
		// Fill arrived before/alongside ack; preserve fill progress and attach broker_order_id idempotently.
		if rec.BrokerOrderID == "" {
			rec.BrokerOrderID = brokerOrderID
		}
		return true, nil
	}
	if rec.Status != domain.OrderPendingSubmit && rec.Status != domain.OrderUnknown {
		return false, domain.NewError(domain.ErrOrderTransitionInvalid, "oms",
			fmt.Sprintf("cannot ack order %s in status %s", clientOrderID, rec.Status), true, false, nil, nil)
	}
	rec.BrokerOrderID = brokerOrderID
	rec.Status = domain.OrderAcked
	rec.UpdatedAtUTC = ts.UTC()
	rec.LastReason = "broker_ack"
	return false, nil
}

// ApplyFill applies an execution fill idempotently by fill_id and prevents overfills.
func (o *OMS) ApplyFill(fill domain.FillRecord) (bool, error) {
	o.mu.Lock()
	defer o.mu.Unlock()

	if o.appliedFills[fill.FillID] {
		return true, nil
	}
	rec, ok := o.orders[fill.ClientOrderID]
	if !ok {
		return false, domain.NewError(domain.ErrNotFound, "oms", fmt.Sprintf("order %s not found for fill %s", fill.ClientOrderID, fill.FillID), true, false, nil, nil)
	}
	if rec.Status == domain.OrderCanceled || rec.Status == domain.OrderRejected {
		return false, domain.NewError(domain.ErrOrderTransitionInvalid, "oms",
			fmt.Sprintf("cannot apply fill %s to terminal order %s (%s)", fill.FillID, fill.ClientOrderID, rec.Status), true, false, nil, nil)
	}
	if rec.FilledQuantity+fill.Quantity > rec.Quantity {
		return false, domain.NewError(domain.ErrOrderTransitionInvalid, "oms",
			fmt.Sprintf("fill %s would overfill order %s (%d + %d > %d)", fill.FillID, fill.ClientOrderID, rec.FilledQuantity, fill.Quantity, rec.Quantity),
			true, false, nil, nil)
	}
	if fill.BrokerOrderID != "" && rec.BrokerOrderID == "" {
		rec.BrokerOrderID = fill.BrokerOrderID
	}
	rec.FilledQuantity += fill.Quantity
	if rec.FilledQuantity == rec.Quantity {
		rec.Status = domain.OrderFilled
	} else {
		rec.Status = domain.OrderPartiallyFilled
	}
	rec.UpdatedAtUTC = fill.ExecutedAtUTC.UTC()
	o.appliedFills[fill.FillID] = true
	o.fills = append(o.fills, fill)
	return false, nil
}

// ApplyCancel transitions an open or uncertain order to CANCELED.
func (o *OMS) ApplyCancel(clientOrderID, reason string, ts time.Time) (bool, error) {
	o.mu.Lock()
	defer o.mu.Unlock()

	rec, ok := o.orders[clientOrderID]
	if !ok {
		return false, domain.NewError(domain.ErrNotFound, "oms", fmt.Sprintf("order %s not found for cancel", clientOrderID), true, false, nil, nil)
	}
	if rec.Status == domain.OrderCanceled {
		return true, nil
	}
	if rec.Status == domain.OrderFilled || rec.Status == domain.OrderRejected {
		return false, domain.NewError(domain.ErrOrderTransitionInvalid, "oms",
			fmt.Sprintf("cannot cancel order %s in terminal status %s", clientOrderID, rec.Status), true, false, nil, nil)
	}
	rec.Status = domain.OrderCanceled
	rec.LastReason = reason
	rec.UpdatedAtUTC = ts.UTC()
	return false, nil
}

// ApplyReject transitions a pending or uncertain order to REJECTED.
func (o *OMS) ApplyReject(clientOrderID, reason string, ts time.Time) (bool, error) {
	o.mu.Lock()
	defer o.mu.Unlock()

	rec, ok := o.orders[clientOrderID]
	if !ok {
		return false, domain.NewError(domain.ErrNotFound, "oms", fmt.Sprintf("order %s not found for reject", clientOrderID), true, false, nil, nil)
	}
	if rec.Status == domain.OrderRejected {
		return true, nil
	}
	if rec.Status != domain.OrderPendingSubmit && rec.Status != domain.OrderUnknown && rec.Status != domain.OrderAcked {
		return false, domain.NewError(domain.ErrOrderTransitionInvalid, "oms",
			fmt.Sprintf("cannot reject order %s in status %s", clientOrderID, rec.Status), true, false, nil, nil)
	}
	rec.Status = domain.OrderRejected
	rec.LastReason = reason
	rec.UpdatedAtUTC = ts.UTC()
	return false, nil
}

// MarkOrderUnknown flags a specific order as UNKNOWN (uncertain external state).
func (o *OMS) MarkOrderUnknown(clientOrderID, reason string, ts time.Time) error {
	o.mu.Lock()
	defer o.mu.Unlock()

	rec, ok := o.orders[clientOrderID]
	if !ok {
		return domain.NewError(domain.ErrNotFound, "oms", fmt.Sprintf("order %s not found", clientOrderID), true, false, nil, nil)
	}
	if rec.Status.IsTerminal() {
		return nil
	}
	rec.Status = domain.OrderUnknown
	rec.LastReason = reason
	rec.UpdatedAtUTC = ts.UTC()
	return nil
}

// MarkPendingAsUnknown transitions all PENDING_SUBMIT orders to UNKNOWN during crash recovery,
// ensuring they cannot be blindly resubmitted without broker reconciliation.
func (o *OMS) MarkPendingAsUnknown(reason string, ts time.Time) []string {
	o.mu.Lock()
	defer o.mu.Unlock()

	marked := make([]string, 0)
	for id, rec := range o.orders {
		if rec.Status == domain.OrderPendingSubmit {
			rec.Status = domain.OrderUnknown
			rec.LastReason = reason
			rec.UpdatedAtUTC = ts.UTC()
			marked = append(marked, id)
		}
	}
	sort.Strings(marked)
	return marked
}

// UncertainOrders returns all orders in PENDING_SUBMIT or UNKNOWN state.
func (o *OMS) UncertainOrders() []domain.OrderRecord {
	o.mu.RLock()
	defer o.mu.RUnlock()
	out := make([]domain.OrderRecord, 0)
	for _, rec := range o.orders {
		if rec.Status == domain.OrderUnknown || rec.Status == domain.OrderPendingSubmit {
			out = append(out, *rec)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ClientOrderID < out[j].ClientOrderID })
	return out
}

// GetOrder returns a copy of an OrderRecord by client_order_id.
func (o *OMS) GetOrder(clientOrderID string) (domain.OrderRecord, bool) {
	o.mu.RLock()
	defer o.mu.RUnlock()
	rec, ok := o.orders[clientOrderID]
	if !ok {
		return domain.OrderRecord{}, false
	}
	return *rec, true
}

// AllOrders returns all orders sorted by ClientOrderID.
func (o *OMS) AllOrders() []domain.OrderRecord {
	o.mu.RLock()
	defer o.mu.RUnlock()
	out := make([]domain.OrderRecord, 0, len(o.orders))
	for _, rec := range o.orders {
		out = append(out, *rec)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ClientOrderID < out[j].ClientOrderID })
	return out
}

// FillCount returns the total number of distinct fills applied.
func (o *OMS) FillCount() int {
	o.mu.RLock()
	defer o.mu.RUnlock()
	return len(o.fills)
}

// OrderCount returns the total number of distinct orders tracked.
func (o *OMS) OrderCount() int {
	o.mu.RLock()
	defer o.mu.RUnlock()
	return len(o.orders)
}

// Snapshot returns a deep copy of the OMS state.
func (o *OMS) Snapshot() domain.OMSSnapshot {
	o.mu.RLock()
	defer o.mu.RUnlock()

	snap := domain.OMSSnapshot{
		Orders:          make(map[string]*domain.OrderRecord, len(o.orders)),
		IdempotencyKeys: make(map[string]string, len(o.idempotencyKeys)),
		AppliedFills:    make(map[string]bool, len(o.appliedFills)),
		Fills:           make([]domain.FillRecord, len(o.fills)),
	}
	for k, v := range o.orders {
		cp := *v
		snap.Orders[k] = &cp
	}
	for k, v := range o.idempotencyKeys {
		snap.IdempotencyKeys[k] = v
	}
	for k, v := range o.appliedFills {
		snap.AppliedFills[k] = v
	}
	copy(snap.Fills, o.fills)
	return snap
}

// Restore replaces the OMS state from snap.
func (o *OMS) Restore(snap domain.OMSSnapshot) error {
	o.mu.Lock()
	defer o.mu.Unlock()

	o.orders = make(map[string]*domain.OrderRecord, len(snap.Orders))
	for k, v := range snap.Orders {
		if v == nil {
			continue
		}
		cp := *v
		o.orders[k] = &cp
	}
	o.idempotencyKeys = make(map[string]string, len(snap.IdempotencyKeys))
	for k, v := range snap.IdempotencyKeys {
		o.idempotencyKeys[k] = v
	}
	o.appliedFills = make(map[string]bool, len(snap.AppliedFills))
	for k, v := range snap.AppliedFills {
		o.appliedFills[k] = v
	}
	o.fills = make([]domain.FillRecord, len(snap.Fills))
	copy(o.fills, snap.Fills)
	return nil
}
