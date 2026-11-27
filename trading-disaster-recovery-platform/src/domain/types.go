package domain

import (
	"fmt"
	"strings"
	"time"
)

// EventType enumerates canonical event types persisted in the WAL.
type EventType string

const (
	EventCashDeposit     EventType = "CASH_DEPOSIT"
	EventCashWithdrawal  EventType = "CASH_WITHDRAWAL"
	EventMarketSnapshot  EventType = "MARKET_SNAPSHOT"
	EventOrderIntent     EventType = "ORDER_INTENT"
	EventOrderAck        EventType = "ORDER_ACK"
	EventOrderFill       EventType = "ORDER_FILL"
	EventOrderCancel     EventType = "ORDER_CANCEL"
	EventOrderReject     EventType = "ORDER_REJECT"
	EventOrderMarkUnk    EventType = "ORDER_MARK_UNKNOWN"
)

// Side represents buy or sell order direction.
type Side string

const (
	SideBuy  Side = "BUY"
	SideSell Side = "SELL"
)

// OrderStatus tracks the deterministic lifecycle of an order in the OMS.
type OrderStatus string

const (
	OrderNew             OrderStatus = "NEW"
	OrderPendingSubmit   OrderStatus = "PENDING_SUBMIT"
	OrderUnknown         OrderStatus = "UNKNOWN"
	OrderAcked           OrderStatus = "ACKED"
	OrderPartiallyFilled OrderStatus = "PARTIALLY_FILLED"
	OrderFilled          OrderStatus = "FILLED"
	OrderCanceled        OrderStatus = "CANCELED"
	OrderRejected        OrderStatus = "REJECTED"
)

// IsTerminal reports whether the order status is final.
func (s OrderStatus) IsTerminal() bool {
	return s == OrderFilled || s == OrderCanceled || s == OrderRejected
}

// TradingEvent is the canonical versioned event envelope stored in fixtures and WAL segments.
// All monetary values use explicit ISO-4217 currency codes and integer minor units (e.g., USD cents).
type TradingEvent struct {
	EventID          string    `json:"event_id"`
	Sequence         uint64    `json:"sequence"`
	EventType        EventType `json:"event_type"`
	EventTimeUTC     time.Time `json:"event_time_utc"`
	AvailableTimeUTC time.Time `json:"available_time_utc"`
	AccountID        string    `json:"account_id,omitempty"`
	ClientOrderID    string    `json:"client_order_id,omitempty"`
	BrokerOrderID    string    `json:"broker_order_id,omitempty"`
	FillID           string    `json:"fill_id,omitempty"`
	Symbol           string    `json:"symbol,omitempty"`
	Side             Side      `json:"side,omitempty"`
	Quantity         int64     `json:"quantity,omitempty"`
	LimitPriceMinor  int64     `json:"limit_price_minor,omitempty"`
	PriceMinor       int64     `json:"price_minor,omitempty"`
	AmountMinor      int64     `json:"amount_minor,omitempty"`
	FeeMinor         int64     `json:"fee_minor,omitempty"`
	Currency         string    `json:"currency,omitempty"`
	IdempotencyKey   string    `json:"idempotency_key"`
	Source           string    `json:"source,omitempty"`
	Reason           string    `json:"reason,omitempty"`
	FencingEpoch     uint64    `json:"fencing_epoch,omitempty"`
}

// Validate checks domain invariants for a TradingEvent.
func (e TradingEvent) Validate() error {
	if strings.TrimSpace(e.EventID) == "" {
		return NewError(ErrInvalidInput, "domain", "event_id is required", true, false, nil, nil)
	}
	if e.Sequence == 0 {
		return NewError(ErrInvalidInput, "domain", "sequence must be > 0", true, false, nil, map[string]any{"event_id": e.EventID})
	}
	if e.EventTimeUTC.IsZero() || e.AvailableTimeUTC.IsZero() {
		return NewError(ErrInvalidInput, "domain", "event_time_utc and available_time_utc are required", true, false, nil, map[string]any{"event_id": e.EventID})
	}
	if e.AvailableTimeUTC.Before(e.EventTimeUTC) {
		return NewError(ErrInvalidInput, "domain", "available_time_utc cannot precede event_time_utc", true, false, nil, map[string]any{
			"event_id": e.EventID,
		})
	}
	if strings.TrimSpace(e.IdempotencyKey) == "" {
		return NewError(ErrInvalidInput, "domain", "idempotency_key is required", true, false, nil, map[string]any{"event_id": e.EventID})
	}

	switch e.EventType {
	case EventCashDeposit, EventCashWithdrawal:
		if e.AccountID == "" || len(e.Currency) != 3 || e.AmountMinor <= 0 {
			return NewError(ErrInvalidInput, "domain", "cash event requires account_id, 3-letter currency, and positive amount_minor", true, false, nil, map[string]any{"event_id": e.EventID})
		}
	case EventMarketSnapshot:
		if e.Symbol == "" || len(e.Currency) != 3 || e.PriceMinor <= 0 {
			return NewError(ErrInvalidInput, "domain", "market snapshot requires symbol, 3-letter currency, and positive price_minor", true, false, nil, map[string]any{"event_id": e.EventID})
		}
	case EventOrderIntent:
		if e.AccountID == "" || e.ClientOrderID == "" || e.Symbol == "" || (e.Side != SideBuy && e.Side != SideSell) || e.Quantity <= 0 || len(e.Currency) != 3 {
			return NewError(ErrInvalidInput, "domain", "order intent missing required fields", true, false, nil, map[string]any{"event_id": e.EventID})
		}
	case EventOrderAck:
		if e.ClientOrderID == "" || e.BrokerOrderID == "" {
			return NewError(ErrInvalidInput, "domain", "order ack requires client_order_id and broker_order_id", true, false, nil, map[string]any{"event_id": e.EventID})
		}
	case EventOrderFill:
		if e.AccountID == "" || e.ClientOrderID == "" || e.FillID == "" || e.Symbol == "" || (e.Side != SideBuy && e.Side != SideSell) || e.Quantity <= 0 || e.PriceMinor <= 0 || e.FeeMinor < 0 || len(e.Currency) != 3 {
			return NewError(ErrInvalidInput, "domain", "order fill missing required fields", true, false, nil, map[string]any{"event_id": e.EventID})
		}
	case EventOrderCancel, EventOrderReject, EventOrderMarkUnk:
		if e.ClientOrderID == "" {
			return NewError(ErrInvalidInput, "domain", "order lifecycle update requires client_order_id", true, false, nil, map[string]any{"event_id": e.EventID})
		}
	default:
		return NewError(ErrInvalidInput, "domain", fmt.Sprintf("unsupported event_type %q", e.EventType), true, false, nil, map[string]any{"event_id": e.EventID})
	}
	return nil
}

// OrderRecord represents an order's durable state in the OMS.
type OrderRecord struct {
	ClientOrderID   string      `json:"client_order_id"`
	BrokerOrderID   string      `json:"broker_order_id,omitempty"`
	IdempotencyKey  string      `json:"idempotency_key"`
	AccountID       string      `json:"account_id"`
	Symbol          string      `json:"symbol"`
	Side            Side        `json:"side"`
	Quantity        int64       `json:"quantity"`
	FilledQuantity  int64       `json:"filled_quantity"`
	LimitPriceMinor int64       `json:"limit_price_minor"`
	Currency        string      `json:"currency"`
	Status          OrderStatus `json:"status"`
	FencingEpoch    uint64      `json:"fencing_epoch"`
	CreatedAtUTC    time.Time   `json:"created_at_utc"`
	UpdatedAtUTC    time.Time   `json:"updated_at_utc"`
	LastReason      string      `json:"last_reason,omitempty"`
}

// FillRecord represents an execution fill applied to OMS and Ledger.
type FillRecord struct {
	FillID        string    `json:"fill_id"`
	ClientOrderID string    `json:"client_order_id"`
	BrokerOrderID string    `json:"broker_order_id,omitempty"`
	AccountID     string    `json:"account_id"`
	Symbol        string    `json:"symbol"`
	Side          Side      `json:"side"`
	Quantity      int64     `json:"quantity"`
	PriceMinor    int64     `json:"price_minor"`
	FeeMinor      int64     `json:"fee_minor"`
	Currency      string    `json:"currency"`
	ExecutedAtUTC time.Time `json:"executed_at_utc"`
}

// MarketSnapshot tracks the latest observed market price and availability timestamp per symbol.
type MarketSnapshot struct {
	Symbol           string    `json:"symbol"`
	PriceMinor       int64     `json:"price_minor"`
	Currency         string    `json:"currency"`
	EventTimeUTC     time.Time `json:"event_time_utc"`
	AvailableTimeUTC time.Time `json:"available_time_utc"`
}

// LedgerEntry records every cash/position mutation for auditability.
type LedgerEntry struct {
	EntryID       string    `json:"entry_id"`
	EventID       string    `json:"event_id"`
	AccountID     string    `json:"account_id"`
	EntryType     string    `json:"entry_type"`
	Currency      string    `json:"currency"`
	CashDelta     int64     `json:"cash_delta_minor"`
	Symbol        string    `json:"symbol,omitempty"`
	PositionDelta int64     `json:"position_delta,omitempty"`
	FeeMinor      int64     `json:"fee_minor,omitempty"`
	TimestampUTC  time.Time `json:"timestamp_utc"`
}

// LedgerSnapshot is the serializable deterministic state of the Ledger.
type LedgerSnapshot struct {
	CashBalances   map[string]map[string]int64 `json:"cash_balances_minor"`
	Positions      map[string]map[string]int64 `json:"positions"`
	AppliedFills   map[string]bool             `json:"applied_fills"`
	AppliedEvents  map[string]bool             `json:"applied_events"`
	TotalFeesMinor map[string]int64            `json:"total_fees_minor"`
	Entries        []LedgerEntry               `json:"entries"`
}

// OMSSnapshot is the serializable deterministic state of the OMS.
type OMSSnapshot struct {
	Orders          map[string]*OrderRecord `json:"orders"`
	IdempotencyKeys map[string]string       `json:"idempotency_keys"`
	AppliedFills    map[string]bool         `json:"applied_fills"`
	Fills           []FillRecord            `json:"fills"`
}

// CheckpointState is the full recoverable state persisted in checkpoints and backups.
type CheckpointState struct {
	SchemaVersion      string                    `json:"schema_version"`
	CheckpointID       string                    `json:"checkpoint_id"`
	CreatedAtUTC       time.Time                 `json:"created_at_utc"`
	LastSequence       uint64                    `json:"last_sequence"`
	LastEventTimeUTC   time.Time                 `json:"last_event_time_utc"`
	LastWALHash        string                    `json:"last_wal_hash"`
	FencingEpoch       uint64                    `json:"fencing_epoch"`
	ProcessedEventIDs  map[string]bool           `json:"processed_event_ids"`
	MarketSnapshots    map[string]MarketSnapshot `json:"market_snapshots"`
	Ledger             LedgerSnapshot            `json:"ledger"`
	OMS                OMSSnapshot               `json:"oms"`
}

// Lease represents an exclusive primary fencing lease.
type Lease struct {
	HolderID      string    `json:"holder_id"`
	Epoch         uint64    `json:"epoch"`
	AcquiredAtUTC time.Time `json:"acquired_at_utc"`
	ExpiresAtUTC  time.Time `json:"expires_at_utc"`
}

// ExecutionGateStatus reports whether order submission is permitted and why.
type ExecutionGateStatus struct {
	Allowed              bool      `json:"allowed"`
	Mode                 string    `json:"mode"` // DISABLED, PAPER_SANDBOX
	FencingEpoch         uint64    `json:"fencing_epoch"`
	CheckedAtUTC         time.Time `json:"checked_at_utc"`
	BlockingReasons      []string  `json:"blocking_reasons,omitempty"`
	ReconciliationPassed bool      `json:"reconciliation_passed"`
	FreshnessPassed      bool      `json:"freshness_passed"`
	FenceValid           bool      `json:"fence_valid"`
}

// HealthMetrics captures operational health, failure, and recovery counters.
type HealthMetrics struct {
	EventsApplied            uint64 `json:"events_applied"`
	DuplicateEventsIgnored   uint64 `json:"duplicate_events_ignored"`
	DuplicateFillsIgnored    uint64 `json:"duplicate_fills_ignored"`
	UncertainOrdersDetected  uint64 `json:"uncertain_orders_detected"`
	UncertainOrdersResolved  uint64 `json:"uncertain_orders_resolved"`
	FencingRejections        uint64 `json:"fencing_rejections"`
	StaleDataBlocks          uint64 `json:"stale_data_blocks"`
	CorruptionDetections     uint64 `json:"corruption_detections"`
	BrokerOutageBlocks       uint64 `json:"broker_outage_blocks"`
	ReconciliationSuccesses  uint64 `json:"reconciliation_successes"`
	ReconciliationFailures   uint64 `json:"reconciliation_failures"`
	CheckpointsWritten       uint64 `json:"checkpoints_written"`
	BackupsRestored          uint64 `json:"backups_restored"`
}
