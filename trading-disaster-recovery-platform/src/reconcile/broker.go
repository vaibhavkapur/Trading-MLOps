package reconcile

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
	"time"

	"trading-dr-platform/src/domain"
)

// BrokerOrder represents the external broker's ground-truth state for an order.
type BrokerOrder struct {
	ClientOrderID  string              `json:"client_order_id"`
	BrokerOrderID  string              `json:"broker_order_id"`
	Symbol         string              `json:"symbol"`
	Side           domain.Side         `json:"side"`
	Quantity       int64               `json:"quantity"`
	FilledQuantity int64               `json:"filled_quantity"`
	Status         domain.OrderStatus  `json:"status"`
	Currency       string              `json:"currency"`
	Fills          []domain.FillRecord `json:"fills,omitempty"`
}

// BrokerAccountSnapshot represents the external broker's authoritative balances, positions, and orders.
type BrokerAccountSnapshot struct {
	AccountID         string           `json:"account_id"`
	AsOfUTC           time.Time        `json:"as_of_utc"`
	CashBalancesMinor map[string]int64 `json:"cash_balances_minor"`
	Positions         map[string]int64 `json:"positions"`
	Orders            []BrokerOrder    `json:"orders"`
}

// BrokerAdapter isolates external broker connectivity behind a clean, testable contract.
type BrokerAdapter interface {
	GetAccountSnapshot(accountID string, fencingEpoch uint64) (BrokerAccountSnapshot, error)
	GetOrderByClientID(clientOrderID string, fencingEpoch uint64) (*BrokerOrder, error)
	SubmitOrder(order domain.OrderRecord, fencingEpoch uint64) (string, error)
}

// SimulatedBroker is a deterministic paper/sandbox broker implementation with fault injection
// and monotonic fencing-epoch enforcement.
type SimulatedBroker struct {
	mu               sync.Mutex
	accountID        string
	asOfUTC          time.Time
	cashBalances     map[string]int64
	positions        map[string]int64
	orders           map[string]*BrokerOrder
	minAcceptedEpoch uint64
	unavailable      bool
	outageReason     string
	nextOrderNum     int
	submittedCalls   int
}

// NewSimulatedBroker creates a SimulatedBroker initialized from baseline balances/positions.
func NewSimulatedBroker(accountID string, asOfUTC time.Time) *SimulatedBroker {
	return &SimulatedBroker{
		accountID:    accountID,
		asOfUTC:      asOfUTC.UTC(),
		cashBalances: make(map[string]int64),
		positions:    make(map[string]int64),
		orders:       make(map[string]*BrokerOrder),
		nextOrderNum: 9100,
	}
}

// LoadSimulatedBrokerFromFile loads a broker baseline fixture JSON into a SimulatedBroker.
func LoadSimulatedBrokerFromFile(path string) (*SimulatedBroker, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, domain.NewError(domain.ErrInvalidInput, "broker", "failed reading broker baseline file", true, false, err, map[string]any{"path": path})
	}
	var snap BrokerAccountSnapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		return nil, domain.NewError(domain.ErrInvalidInput, "broker", "failed parsing broker baseline JSON", true, false, err, map[string]any{"path": path})
	}
	b := NewSimulatedBroker(snap.AccountID, snap.AsOfUTC)
	for k, v := range snap.CashBalancesMinor {
		b.cashBalances[k] = v
	}
	for k, v := range snap.Positions {
		b.positions[k] = v
	}
	for i := range snap.Orders {
		ord := snap.Orders[i]
		cp := ord
		b.orders[ord.ClientOrderID] = &cp
	}
	return b, nil
}

// SetUnavailable injects or clears a broker outage / connectivity failure.
func (b *SimulatedBroker) SetUnavailable(unavailable bool, reason string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.unavailable = unavailable
	b.outageReason = reason
}

// SetAsOfUTC sets the broker snapshot timestamp.
func (b *SimulatedBroker) SetAsOfUTC(ts time.Time) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.asOfUTC = ts.UTC()
}

// AdvanceFenceEpoch simulates the broker gateway observing a higher fencing epoch from the newly promoted primary.
func (b *SimulatedBroker) AdvanceFenceEpoch(epoch uint64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if epoch > b.minAcceptedEpoch {
		b.minAcceptedEpoch = epoch
	}
}

// InjectCashDelta adjusts broker cash directly (e.g., to simulate external drift or reconciliation tests).
func (b *SimulatedBroker) InjectCashDelta(currency string, deltaMinor int64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.cashBalances[currency] += deltaMinor
}

// InjectPositionDelta adjusts broker position directly.
func (b *SimulatedBroker) InjectPositionDelta(symbol string, deltaQty int64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.positions[symbol] += deltaQty
}

// InjectOrderAndFill simulates an order that reached the broker (e.g., before local crash mid-submit)
// and optionally executed a fill, updating broker cash and position balances deterministically.
func (b *SimulatedBroker) InjectOrderAndFill(ord BrokerOrder, fill *domain.FillRecord) {
	b.mu.Lock()
	defer b.mu.Unlock()
	cp := ord
	if fill != nil {
		cp.Fills = append([]domain.FillRecord(nil), *fill)
		cp.FilledQuantity = fill.Quantity
		if cp.FilledQuantity >= cp.Quantity {
			cp.Status = domain.OrderFilled
		} else {
			cp.Status = domain.OrderPartiallyFilled
		}
		notional := fill.Quantity * fill.PriceMinor
		if fill.Side == domain.SideBuy {
			b.cashBalances[fill.Currency] -= (notional + fill.FeeMinor)
			b.positions[fill.Symbol] += fill.Quantity
		} else {
			b.cashBalances[fill.Currency] += (notional - fill.FeeMinor)
			b.positions[fill.Symbol] -= fill.Quantity
		}
	}
	b.orders[cp.ClientOrderID] = &cp
}

// SubmittedCallCount returns the number of accepted SubmitOrder calls (for duplicate-submit verification).
func (b *SimulatedBroker) SubmittedCallCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.submittedCalls
}

func (b *SimulatedBroker) checkAvailabilityAndFenceLocked(epoch uint64) error {
	if b.unavailable {
		msg := b.outageReason
		if msg == "" {
			msg = "broker API unreachable"
		}
		return domain.NewError(domain.ErrBrokerUnavailable, "broker", msg, true, true, nil, nil)
	}
	if epoch == 0 {
		return domain.NewError(domain.ErrFencingViolation, "broker", "broker call rejected: fencing epoch must be > 0", true, false, nil, nil)
	}
	if epoch < b.minAcceptedEpoch {
		return domain.NewError(domain.ErrSplitBrainDetected, "broker",
			fmt.Sprintf("broker rejected stale fencing epoch %d < active epoch %d", epoch, b.minAcceptedEpoch),
			true, false, nil, map[string]any{"caller_epoch": epoch, "min_epoch": b.minAcceptedEpoch})
	}
	b.minAcceptedEpoch = epoch
	return nil
}

// GetAccountSnapshot returns the broker's authoritative snapshot for accountID.
func (b *SimulatedBroker) GetAccountSnapshot(accountID string, fencingEpoch uint64) (BrokerAccountSnapshot, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.checkAvailabilityAndFenceLocked(fencingEpoch); err != nil {
		return BrokerAccountSnapshot{}, err
	}
	if accountID != b.accountID {
		return BrokerAccountSnapshot{}, domain.NewError(domain.ErrNotFound, "broker", fmt.Sprintf("unknown broker account %s", accountID), true, false, nil, nil)
	}
	cash := make(map[string]int64, len(b.cashBalances))
	for k, v := range b.cashBalances {
		cash[k] = v
	}
	pos := make(map[string]int64, len(b.positions))
	for k, v := range b.positions {
		if v != 0 {
			pos[k] = v
		}
	}
	ords := make([]BrokerOrder, 0, len(b.orders))
	for _, o := range b.orders {
		cp := *o
		cp.Fills = append([]domain.FillRecord(nil), o.Fills...)
		ords = append(ords, cp)
	}
	sort.Slice(ords, func(i, j int) bool { return ords[i].ClientOrderID < ords[j].ClientOrderID })
	return BrokerAccountSnapshot{
		AccountID:         b.accountID,
		AsOfUTC:           b.asOfUTC,
		CashBalancesMinor: cash,
		Positions:         pos,
		Orders:            ords,
	}, nil
}

// GetOrderByClientID looks up an order by client_order_id at the broker. Returns (nil, nil) if never received.
func (b *SimulatedBroker) GetOrderByClientID(clientOrderID string, fencingEpoch uint64) (*BrokerOrder, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.checkAvailabilityAndFenceLocked(fencingEpoch); err != nil {
		return nil, err
	}
	o, ok := b.orders[clientOrderID]
	if !ok {
		return nil, nil
	}
	cp := *o
	cp.Fills = append([]domain.FillRecord(nil), o.Fills...)
	return &cp, nil
}

// SubmitOrder accepts an idempotent order submission gated by fencingEpoch.
func (b *SimulatedBroker) SubmitOrder(order domain.OrderRecord, fencingEpoch uint64) (string, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.checkAvailabilityAndFenceLocked(fencingEpoch); err != nil {
		return "", err
	}
	if existing, ok := b.orders[order.ClientOrderID]; ok {
		return existing.BrokerOrderID, nil
	}
	b.nextOrderNum++
	b.submittedCalls++
	brkID := fmt.Sprintf("BRK-%d", b.nextOrderNum)
	b.orders[order.ClientOrderID] = &BrokerOrder{
		ClientOrderID:  order.ClientOrderID,
		BrokerOrderID:  brkID,
		Symbol:         order.Symbol,
		Side:           order.Side,
		Quantity:       order.Quantity,
		FilledQuantity: 0,
		Status:         domain.OrderAcked,
		Currency:       order.Currency,
	}
	return brkID, nil
}
