package ledger

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"trading-dr-platform/src/domain"
)

// Ledger maintains deterministic cash balances (minor units), positions, and an immutable posting journal.
type Ledger struct {
	mu             sync.RWMutex
	cashBalances   map[string]map[string]int64 // account -> currency -> minor units
	positions      map[string]map[string]int64 // account -> symbol -> qty
	appliedFills   map[string]bool
	appliedEvents  map[string]bool
	totalFeesMinor map[string]int64 // currency -> total fees minor units
	entries        []domain.LedgerEntry
}

// New creates an empty Ledger.
func New() *Ledger {
	return &Ledger{
		cashBalances:   make(map[string]map[string]int64),
		positions:      make(map[string]map[string]int64),
		appliedFills:   make(map[string]bool),
		appliedEvents:  make(map[string]bool),
		totalFeesMinor: make(map[string]int64),
		entries:        make([]domain.LedgerEntry, 0),
	}
}

func (l *Ledger) ensureAccountMaps(accountID string) {
	if _, ok := l.cashBalances[accountID]; !ok {
		l.cashBalances[accountID] = make(map[string]int64)
	}
	if _, ok := l.positions[accountID]; !ok {
		l.positions[accountID] = make(map[string]int64)
	}
}

// ApplyCashDeposit posts an idempotent cash deposit. Returns (duplicate, error).
func (l *Ledger) ApplyCashDeposit(eventID, accountID, currency string, amountMinor int64, ts time.Time) (bool, error) {
	if accountID == "" || len(currency) != 3 || amountMinor <= 0 {
		return false, domain.NewError(domain.ErrInvalidInput, "ledger", "invalid cash deposit arguments", true, false, nil, nil)
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	if l.appliedEvents[eventID] {
		return true, nil
	}
	l.ensureAccountMaps(accountID)
	l.cashBalances[accountID][currency] += amountMinor
	l.appliedEvents[eventID] = true
	l.entries = append(l.entries, domain.LedgerEntry{
		EntryID:      fmt.Sprintf("le-%06d", len(l.entries)+1),
		EventID:      eventID,
		AccountID:    accountID,
		EntryType:    "CASH_DEPOSIT",
		Currency:     currency,
		CashDelta:    amountMinor,
		TimestampUTC: ts.UTC(),
	})
	return false, nil
}

// ApplyCashWithdrawal posts an idempotent cash withdrawal. Returns (duplicate, error).
func (l *Ledger) ApplyCashWithdrawal(eventID, accountID, currency string, amountMinor int64, ts time.Time) (bool, error) {
	if accountID == "" || len(currency) != 3 || amountMinor <= 0 {
		return false, domain.NewError(domain.ErrInvalidInput, "ledger", "invalid cash withdrawal arguments", true, false, nil, nil)
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	if l.appliedEvents[eventID] {
		return true, nil
	}
	l.ensureAccountMaps(accountID)
	l.cashBalances[accountID][currency] -= amountMinor
	l.appliedEvents[eventID] = true
	l.entries = append(l.entries, domain.LedgerEntry{
		EntryID:      fmt.Sprintf("le-%06d", len(l.entries)+1),
		EventID:      eventID,
		AccountID:    accountID,
		EntryType:    "CASH_WITHDRAWAL",
		Currency:     currency,
		CashDelta:    -amountMinor,
		TimestampUTC: ts.UTC(),
	})
	return false, nil
}

// ApplyFill atomically updates cash, position, and fees for a fill, deduplicating on both eventID and fillID.
// Returns (duplicate, error).
func (l *Ledger) ApplyFill(eventID string, fill domain.FillRecord) (bool, error) {
	if fill.FillID == "" || fill.AccountID == "" || fill.Symbol == "" || fill.Quantity <= 0 || fill.PriceMinor <= 0 || fill.FeeMinor < 0 || len(fill.Currency) != 3 {
		return false, domain.NewError(domain.ErrInvalidInput, "ledger", "invalid fill record", true, false, nil, map[string]any{"fill_id": fill.FillID})
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	if l.appliedFills[fill.FillID] || (eventID != "" && l.appliedEvents[eventID]) {
		return true, nil
	}

	l.ensureAccountMaps(fill.AccountID)
	notional := fill.Quantity * fill.PriceMinor
	var cashDelta int64
	var posDelta int64
	switch fill.Side {
	case domain.SideBuy:
		cashDelta = -(notional + fill.FeeMinor)
		posDelta = fill.Quantity
	case domain.SideSell:
		cashDelta = notional - fill.FeeMinor
		posDelta = -fill.Quantity
	default:
		return false, domain.NewError(domain.ErrInvalidInput, "ledger", fmt.Sprintf("unsupported side %q", fill.Side), true, false, nil, nil)
	}

	l.cashBalances[fill.AccountID][fill.Currency] += cashDelta
	l.positions[fill.AccountID][fill.Symbol] += posDelta
	l.totalFeesMinor[fill.Currency] += fill.FeeMinor
	l.appliedFills[fill.FillID] = true
	if eventID != "" {
		l.appliedEvents[eventID] = true
	}

	l.entries = append(l.entries, domain.LedgerEntry{
		EntryID:       fmt.Sprintf("le-%06d", len(l.entries)+1),
		EventID:       eventID,
		AccountID:     fill.AccountID,
		EntryType:     "ORDER_FILL",
		Currency:      fill.Currency,
		CashDelta:     cashDelta,
		Symbol:        fill.Symbol,
		PositionDelta: posDelta,
		FeeMinor:      fill.FeeMinor,
		TimestampUTC:  fill.ExecutedAtUTC.UTC(),
	})
	return false, nil
}

// CashBalance returns the cash balance in minor units for an account and currency.
func (l *Ledger) CashBalance(accountID, currency string) int64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	if acct, ok := l.cashBalances[accountID]; ok {
		return acct[currency]
	}
	return 0
}

// CashBalancesForAccount returns a copy of currency -> balance for an account.
func (l *Ledger) CashBalancesForAccount(accountID string) map[string]int64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	out := make(map[string]int64)
	if acct, ok := l.cashBalances[accountID]; ok {
		for k, v := range acct {
			out[k] = v
		}
	}
	return out
}

// Position returns the net position quantity for an account and symbol.
func (l *Ledger) Position(accountID, symbol string) int64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	if acct, ok := l.positions[accountID]; ok {
		return acct[symbol]
	}
	return 0
}

// PositionsForAccount returns a copy of symbol -> position quantity for an account.
func (l *Ledger) PositionsForAccount(accountID string) map[string]int64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	out := make(map[string]int64)
	if acct, ok := l.positions[accountID]; ok {
		for k, v := range acct {
			if v != 0 {
				out[k] = v
			}
		}
	}
	return out
}

// EntryCount returns the number of posted ledger journal entries.
func (l *Ledger) EntryCount() int {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return len(l.entries)
}

// VerifyJournalInvariant recomputes balances from the posting journal and asserts exact equality with live state.
func (l *Ledger) VerifyJournalInvariant() error {
	l.mu.RLock()
	defer l.mu.RUnlock()

	calcCash := make(map[string]map[string]int64)
	calcPos := make(map[string]map[string]int64)
	for _, entry := range l.entries {
		if _, ok := calcCash[entry.AccountID]; !ok {
			calcCash[entry.AccountID] = make(map[string]int64)
		}
		if _, ok := calcPos[entry.AccountID]; !ok {
			calcPos[entry.AccountID] = make(map[string]int64)
		}
		calcCash[entry.AccountID][entry.Currency] += entry.CashDelta
		if entry.Symbol != "" {
			calcPos[entry.AccountID][entry.Symbol] += entry.PositionDelta
		}
	}

	for acct, cMap := range l.cashBalances {
		for ccy, bal := range cMap {
			if calcCash[acct][ccy] != bal {
				return domain.NewError(domain.ErrReconciliationMismatch, "ledger",
					fmt.Sprintf("cash journal invariant failed for %s/%s: state=%d journal=%d", acct, ccy, bal, calcCash[acct][ccy]),
					true, false, nil, nil)
			}
		}
	}
	for acct, pMap := range l.positions {
		for sym, qty := range pMap {
			if calcPos[acct][sym] != qty {
				return domain.NewError(domain.ErrReconciliationMismatch, "ledger",
					fmt.Sprintf("position journal invariant failed for %s/%s: state=%d journal=%d", acct, sym, qty, calcPos[acct][sym]),
					true, false, nil, nil)
			}
		}
	}
	return nil
}

// Snapshot returns a deep copy of the Ledger state suitable for durable checkpointing.
func (l *Ledger) Snapshot() domain.LedgerSnapshot {
	l.mu.RLock()
	defer l.mu.RUnlock()

	snap := domain.LedgerSnapshot{
		CashBalances:   make(map[string]map[string]int64, len(l.cashBalances)),
		Positions:      make(map[string]map[string]int64, len(l.positions)),
		AppliedFills:   make(map[string]bool, len(l.appliedFills)),
		AppliedEvents:  make(map[string]bool, len(l.appliedEvents)),
		TotalFeesMinor: make(map[string]int64, len(l.totalFeesMinor)),
		Entries:        make([]domain.LedgerEntry, len(l.entries)),
	}
	for acct, m := range l.cashBalances {
		snap.CashBalances[acct] = make(map[string]int64, len(m))
		for k, v := range m {
			snap.CashBalances[acct][k] = v
		}
	}
	for acct, m := range l.positions {
		snap.Positions[acct] = make(map[string]int64, len(m))
		for k, v := range m {
			snap.Positions[acct][k] = v
		}
	}
	for k, v := range l.appliedFills {
		snap.AppliedFills[k] = v
	}
	for k, v := range l.appliedEvents {
		snap.AppliedEvents[k] = v
	}
	for k, v := range l.totalFeesMinor {
		snap.TotalFeesMinor[k] = v
	}
	copy(snap.Entries, l.entries)
	return snap
}

// Restore replaces the Ledger state from a deep copy of snap and verifies journal invariants.
func (l *Ledger) Restore(snap domain.LedgerSnapshot) error {
	l.mu.Lock()
	l.cashBalances = make(map[string]map[string]int64, len(snap.CashBalances))
	for acct, m := range snap.CashBalances {
		l.cashBalances[acct] = make(map[string]int64, len(m))
		for k, v := range m {
			l.cashBalances[acct][k] = v
		}
	}
	l.positions = make(map[string]map[string]int64, len(snap.Positions))
	for acct, m := range snap.Positions {
		l.positions[acct] = make(map[string]int64, len(m))
		for k, v := range m {
			l.positions[acct][k] = v
		}
	}
	l.appliedFills = make(map[string]bool, len(snap.AppliedFills))
	for k, v := range snap.AppliedFills {
		l.appliedFills[k] = v
	}
	l.appliedEvents = make(map[string]bool, len(snap.AppliedEvents))
	for k, v := range snap.AppliedEvents {
		l.appliedEvents[k] = v
	}
	l.totalFeesMinor = make(map[string]int64, len(snap.TotalFeesMinor))
	for k, v := range snap.TotalFeesMinor {
		l.totalFeesMinor[k] = v
	}
	l.entries = make([]domain.LedgerEntry, len(snap.Entries))
	copy(l.entries, snap.Entries)
	l.mu.Unlock()

	return l.VerifyJournalInvariant()
}

// Accounts returns a sorted list of account IDs present in the ledger.
func (l *Ledger) Accounts() []string {
	l.mu.RLock()
	defer l.mu.RUnlock()
	seen := make(map[string]bool)
	for a := range l.cashBalances {
		seen[a] = true
	}
	for a := range l.positions {
		seen[a] = true
	}
	out := make([]string, 0, len(seen))
	for a := range seen {
		out = append(out, a)
	}
	sort.Strings(out)
	return out
}
