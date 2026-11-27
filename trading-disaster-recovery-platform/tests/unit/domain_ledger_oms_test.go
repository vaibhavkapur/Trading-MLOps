package unit

import (
	"path/filepath"
	"testing"
	"time"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/ledger"
	"trading-dr-platform/src/oms"
)

func TestPolicyAndFailureModeConfigValidation(t *testing.T) {
	policyPath := filepath.Join("..", "..", "config", "dr_policy.v1.json")
	policy, err := domain.LoadPolicy(policyPath)
	if err != nil {
		t.Fatalf("expected valid policy, got: %v", err)
	}
	if policy.Execution.LiveExecutionAllowed {
		t.Fatalf("live execution must remain disabled in MVP policy")
	}

	// Mutating live_execution_allowed to true must be rejected by Validate().
	badPolicy := policy
	badPolicy.Execution.LiveExecutionAllowed = true
	if err := badPolicy.Validate(); !domain.HasCode(err, domain.ErrPolicyViolation) {
		t.Fatalf("expected ErrPolicyViolation for live_execution_allowed=true, got: %v", err)
	}

	fmPath := filepath.Join("..", "..", "config", "failure_modes.v1.json")
	inv, err := domain.LoadFailureModes(fmPath)
	if err != nil {
		t.Fatalf("expected valid failure mode inventory, got: %v", err)
	}
	if len(inv.FailureModes) != 8 {
		t.Fatalf("expected 8 failure modes, got %d", len(inv.FailureModes))
	}
}

func TestTradingEventValidationInvariants(t *testing.T) {
	now := time.Date(2026, 1, 15, 14, 30, 0, 0, time.UTC)

	// Available time preceding event time must fail validation.
	badTime := domain.TradingEvent{
		EventID:          "evt-bad-time",
		Sequence:         1,
		EventType:        domain.EventCashDeposit,
		EventTimeUTC:     now,
		AvailableTimeUTC: now.Add(-1 * time.Second),
		AccountID:        "ACCT-1",
		Currency:         "USD",
		AmountMinor:      1000,
		IdempotencyKey:   "k1",
	}
	if err := badTime.Validate(); !domain.HasCode(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput for available_time < event_time, got: %v", err)
	}

	// Non-ISO currency code must fail.
	badCcy := badTime
	badCcy.AvailableTimeUTC = now
	badCcy.Currency = "US"
	if err := badCcy.Validate(); !domain.HasCode(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput for invalid currency, got: %v", err)
	}
}

func TestLedgerIdempotencyAndJournalInvariants(t *testing.T) {
	l := ledger.New()
	t0 := time.Date(2026, 1, 15, 14, 30, 0, 0, time.UTC)

	dup, err := l.ApplyCashDeposit("evt-1", "ACCT-1", "USD", 1000000, t0)
	if err != nil || dup {
		t.Fatalf("deposit failed: dup=%v err=%v", dup, err)
	}
	// Duplicate deposit ignored.
	dup, err = l.ApplyCashDeposit("evt-1", "ACCT-1", "USD", 1000000, t0)
	if err != nil || !dup {
		t.Fatalf("expected duplicate deposit to be ignored: dup=%v err=%v", dup, err)
	}

	fill := domain.FillRecord{
		FillID:        "FILL-1",
		ClientOrderID: "ORD-1",
		AccountID:     "ACCT-1",
		Symbol:        "AAPL",
		Side:          domain.SideBuy,
		Quantity:      10,
		PriceMinor:    18500,
		FeeMinor:      50,
		Currency:      "USD",
		ExecutedAtUTC: t0,
	}
	dup, err = l.ApplyFill("evt-2", fill)
	if err != nil || dup {
		t.Fatalf("fill failed: dup=%v err=%v", dup, err)
	}
	// Redelivered fill with different event ID must still be deduplicated by FillID.
	dup, err = l.ApplyFill("evt-2-redelivered", fill)
	if err != nil || !dup {
		t.Fatalf("expected duplicate fill_id to be ignored: dup=%v err=%v", dup, err)
	}

	expectedCash := int64(1000000 - (10*18500 + 50))
	if got := l.CashBalance("ACCT-1", "USD"); got != expectedCash {
		t.Fatalf("cash mismatch: got %d want %d", got, expectedCash)
	}
	if got := l.Position("ACCT-1", "AAPL"); got != 10 {
		t.Fatalf("position mismatch: got %d want 10", got)
	}
	if err := l.VerifyJournalInvariant(); err != nil {
		t.Fatalf("journal invariant failed: %v", err)
	}

	// Snapshot & Restore preserves exact state.
	snap := l.Snapshot()
	l2 := ledger.New()
	if err := l2.Restore(snap); err != nil {
		t.Fatalf("restore failed: %v", err)
	}
	if l2.CashBalance("ACCT-1", "USD") != expectedCash || l2.Position("ACCT-1", "AAPL") != 10 {
		t.Fatalf("restored ledger state mismatch")
	}
}

func TestOMSStateMachineIdempotencyAndSafety(t *testing.T) {
	o := oms.New()
	t0 := time.Date(2026, 1, 15, 14, 30, 0, 0, time.UTC)

	intent := domain.TradingEvent{
		EventID: "evt-o1", Sequence: 1, EventType: domain.EventOrderIntent,
		EventTimeUTC: t0, AvailableTimeUTC: t0, AccountID: "ACCT-1",
		ClientOrderID: "ORD-1", Symbol: "AAPL", Side: domain.SideBuy,
		Quantity: 100, LimitPriceMinor: 18500, Currency: "USD", IdempotencyKey: "idem-1",
	}
	dup, err := o.RecordIntent(intent)
	if err != nil || dup {
		t.Fatalf("record intent failed: dup=%v err=%v", dup, err)
	}
	// Identical intent is idempotent.
	dup, err = o.RecordIntent(intent)
	if err != nil || !dup {
		t.Fatalf("expected idempotent duplicate intent, got dup=%v err=%v", dup, err)
	}
	// Conflicting intent with same idempotency key must fail.
	conflict := intent
	conflict.Quantity = 200
	if _, err := o.RecordIntent(conflict); !domain.HasCode(err, domain.ErrDuplicateEvent) {
		t.Fatalf("expected ErrDuplicateEvent for conflicting idempotency key, got: %v", err)
	}

	// Mark pending as unknown after crash.
	marked := o.MarkPendingAsUnknown("crash", t0)
	if len(marked) != 1 || marked[0] != "ORD-1" {
		t.Fatalf("expected ORD-1 marked unknown, got %v", marked)
	}

	// Ack and fill from UNKNOWN state.
	if _, err := o.ApplyAck("ORD-1", "BRK-1", t0); err != nil {
		t.Fatalf("ack failed: %v", err)
	}
	if _, err := o.ApplyFill(domain.FillRecord{
		FillID: "F1", ClientOrderID: "ORD-1", AccountID: "ACCT-1", Symbol: "AAPL",
		Side: domain.SideBuy, Quantity: 60, PriceMinor: 18500, Currency: "USD", ExecutedAtUTC: t0,
	}); err != nil {
		t.Fatalf("partial fill failed: %v", err)
	}
	// Overfill must be rejected.
	if _, err := o.ApplyFill(domain.FillRecord{
		FillID: "F2", ClientOrderID: "ORD-1", AccountID: "ACCT-1", Symbol: "AAPL",
		Side: domain.SideBuy, Quantity: 50, PriceMinor: 18500, Currency: "USD", ExecutedAtUTC: t0,
	}); !domain.HasCode(err, domain.ErrOrderTransitionInvalid) {
		t.Fatalf("expected ErrOrderTransitionInvalid on overfill, got: %v", err)
	}
	// Exact remaining fill completes order.
	if _, err := o.ApplyFill(domain.FillRecord{
		FillID: "F3", ClientOrderID: "ORD-1", AccountID: "ACCT-1", Symbol: "AAPL",
		Side: domain.SideBuy, Quantity: 40, PriceMinor: 18500, Currency: "USD", ExecutedAtUTC: t0,
	}); err != nil {
		t.Fatalf("final fill failed: %v", err)
	}
	rec, _ := o.GetOrder("ORD-1")
	if rec.Status != domain.OrderFilled {
		t.Fatalf("expected FILLED, got %s", rec.Status)
	}
}
