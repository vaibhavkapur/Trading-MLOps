package reconcile

import (
	"fmt"
	"math"
	"sort"
	"time"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/replay"
)

// OrderResolution documents how an uncertain order was resolved against broker truth.
type OrderResolution struct {
	ClientOrderID   string             `json:"client_order_id"`
	PreviousStatus  domain.OrderStatus `json:"previous_status"`
	ResolvedStatus  domain.OrderStatus `json:"resolved_status"`
	BrokerOrderID   string             `json:"broker_order_id,omitempty"`
	FillsRecovered  int                `json:"fills_recovered"`
	ActionTaken     string             `json:"action_taken"`
}

// BalanceDiff records a discrepancy between local ledger and broker cash or position.
type BalanceDiff struct {
	Kind      string `json:"kind"` // CASH or POSITION
	Key       string `json:"key"`  // currency or symbol
	LocalVal  int64  `json:"local_value"`
	BrokerVal int64  `json:"broker_value"`
	Diff      int64  `json:"diff"`
}

// ReconciliationReport summarizes broker reconciliation outcomes and any blocking discrepancies.
type ReconciliationReport struct {
	AccountID              string            `json:"account_id"`
	ReconciledAtUTC        time.Time         `json:"reconciled_at_utc"`
	BrokerSnapshotAsOfUTC  time.Time         `json:"broker_snapshot_as_of_utc"`
	Passed                 bool              `json:"passed"`
	UncertainOrdersFound   int               `json:"uncertain_orders_found"`
	Resolutions            []OrderResolution `json:"resolutions,omitempty"`
	BalanceDiffs           []BalanceDiff     `json:"balance_diffs,omitempty"`
	UnexpectedBrokerOrders []string          `json:"unexpected_broker_orders,omitempty"`
	BlockingReasons        []string          `json:"blocking_reasons,omitempty"`
}

// Reconciler reconciles recovered local OMS + Ledger state against authoritative broker state.
type Reconciler struct{}

// NewReconciler constructs a Reconciler.
func NewReconciler() *Reconciler {
	return &Reconciler{}
}

// Reconcile queries the broker, resolves uncertain/in-flight orders without duplicate submission,
// verifies broker snapshot freshness, and asserts cash & position convergence within policy tolerances.
func (r *Reconciler) Reconcile(
	accountID string,
	fencingEpoch uint64,
	now time.Time,
	engine *replay.Engine,
	broker BrokerAdapter,
	policy domain.DRPolicy,
) (ReconciliationReport, error) {
	nowUTC := now.UTC()
	report := ReconciliationReport{
		AccountID:       accountID,
		ReconciledAtUTC: nowUTC,
		Passed:          false,
	}

	// 1. Fetch authoritative broker account snapshot.
	snap, err := broker.GetAccountSnapshot(accountID, fencingEpoch)
	if err != nil {
		engine.IncrementMetric(func(m *domain.HealthMetrics) {
			m.ReconciliationFailures++
			if domain.HasCode(err, domain.ErrBrokerUnavailable) {
				m.BrokerOutageBlocks++
			}
			if domain.HasCode(err, domain.ErrSplitBrainDetected) || domain.HasCode(err, domain.ErrFencingViolation) {
				m.FencingRejections++
			}
		})
		report.BlockingReasons = append(report.BlockingReasons, fmt.Sprintf("broker_snapshot_failed: %v", err))
		return report, domain.NewError(domain.ErrUnknownExternalState, "reconcile",
			"cannot verify external broker state; blocking resumption (fail-closed)", true, true, err, nil)
	}
	report.BrokerSnapshotAsOfUTC = snap.AsOfUTC.UTC()

	// 2. Check broker snapshot freshness.
	if policy.Freshness.MaxBrokerSnapshotAgeMS > 0 {
		ageMS := nowUTC.Sub(snap.AsOfUTC.UTC()).Milliseconds()
		if ageMS > policy.Freshness.MaxBrokerSnapshotAgeMS {
			engine.IncrementMetric(func(m *domain.HealthMetrics) {
				m.ReconciliationFailures++
				m.StaleDataBlocks++
			})
			report.BlockingReasons = append(report.BlockingReasons,
				fmt.Sprintf("stale_broker_snapshot: age=%dms > max=%dms", ageMS, policy.Freshness.MaxBrokerSnapshotAgeMS))
			return report, domain.NewError(domain.ErrStaleSnapshot, "reconcile",
				fmt.Sprintf("broker snapshot age %dms exceeds limit %dms", ageMS, policy.Freshness.MaxBrokerSnapshotAgeMS),
				true, true, nil, map[string]any{"age_ms": ageMS, "max_ms": policy.Freshness.MaxBrokerSnapshotAgeMS})
		}
	}

	// 3. Resolve uncertain orders (PENDING_SUBMIT or UNKNOWN) against broker truth without resubmitting.
	uncertain := engine.OMS().UncertainOrders()
	report.UncertainOrdersFound = len(uncertain)
	for _, ord := range uncertain {
		brkOrd, err := broker.GetOrderByClientID(ord.ClientOrderID, fencingEpoch)
		if err != nil {
			engine.IncrementMetric(func(m *domain.HealthMetrics) {
				m.ReconciliationFailures++
				m.BrokerOutageBlocks++
			})
			report.BlockingReasons = append(report.BlockingReasons,
				fmt.Sprintf("order_query_failed(%s): %v", ord.ClientOrderID, err))
			return report, domain.NewError(domain.ErrUnknownExternalState, "reconcile",
				fmt.Sprintf("failed querying uncertain order %s at broker", ord.ClientOrderID), true, true, err, nil)
		}

		res := OrderResolution{
			ClientOrderID:  ord.ClientOrderID,
			PreviousStatus: ord.Status,
		}
		if brkOrd == nil {
			// Order never reached the broker before local crash. Mark locally REJECTED; never auto-resubmit blindly.
			if _, err := engine.OMS().ApplyReject(ord.ClientOrderID, "not_found_at_broker_during_reconciliation", nowUTC); err != nil {
				return report, err
			}
			res.ResolvedStatus = domain.OrderRejected
			res.ActionTaken = "marked_rejected_not_found_at_broker_no_resubmit"
		} else {
			res.BrokerOrderID = brkOrd.BrokerOrderID
			if brkOrd.BrokerOrderID != "" && (brkOrd.Status == domain.OrderAcked || brkOrd.Status == domain.OrderPartiallyFilled || brkOrd.Status == domain.OrderFilled) {
				if _, err := engine.OMS().ApplyAck(ord.ClientOrderID, brkOrd.BrokerOrderID, nowUTC); err != nil {
					return report, err
				}
			}
			for _, fill := range brkOrd.Fills {
				dupOMS, err := engine.OMS().ApplyFill(fill)
				if err != nil {
					return report, err
				}
				dupLedger, err := engine.Ledger().ApplyFill("reconcile-"+fill.FillID, fill)
				if err != nil {
					return report, err
				}
				if !dupOMS || !dupLedger {
					res.FillsRecovered++
				}
			}
			switch brkOrd.Status {
			case domain.OrderCanceled:
				if _, err := engine.OMS().ApplyCancel(ord.ClientOrderID, "reconciled_canceled_at_broker", nowUTC); err != nil {
					return report, err
				}
			case domain.OrderRejected:
				if _, err := engine.OMS().ApplyReject(ord.ClientOrderID, "reconciled_rejected_at_broker", nowUTC); err != nil {
					return report, err
				}
			}
			updated, _ := engine.OMS().GetOrder(ord.ClientOrderID)
			res.ResolvedStatus = updated.Status
			res.ActionTaken = fmt.Sprintf("converged_to_broker_state_%s", updated.Status)
		}
		report.Resolutions = append(report.Resolutions, res)
		engine.IncrementMetric(func(m *domain.HealthMetrics) {
			m.UncertainOrdersResolved++
		})
	}

	// 4. Verify no unexpected broker orders exist that local OMS is unaware of.
	for _, brkOrd := range snap.Orders {
		if _, ok := engine.OMS().GetOrder(brkOrd.ClientOrderID); !ok {
			report.UnexpectedBrokerOrders = append(report.UnexpectedBrokerOrders, brkOrd.ClientOrderID)
		}
	}
	sort.Strings(report.UnexpectedBrokerOrders)
	if len(report.UnexpectedBrokerOrders) > 0 && policy.Reconciliation.AbortOnUnexpectedBrokerOrder {
		report.BlockingReasons = append(report.BlockingReasons,
			fmt.Sprintf("unexpected_broker_orders: %v", report.UnexpectedBrokerOrders))
	}

	// 5. Reconcile cash balances across all currencies.
	localCash := engine.Ledger().CashBalancesForAccount(accountID)
	currencies := make(map[string]bool)
	for c := range localCash {
		currencies[c] = true
	}
	for c := range snap.CashBalancesMinor {
		currencies[c] = true
	}
	ccyList := make([]string, 0, len(currencies))
	for c := range currencies {
		ccyList = append(ccyList, c)
	}
	sort.Strings(ccyList)
	for _, ccy := range ccyList {
		lv := localCash[ccy]
		bv := snap.CashBalancesMinor[ccy]
		diff := lv - bv
		if int64(math.Abs(float64(diff))) > policy.Reconciliation.MaxCashToleranceMinor {
			report.BalanceDiffs = append(report.BalanceDiffs, BalanceDiff{
				Kind:      "CASH",
				Key:       ccy,
				LocalVal:  lv,
				BrokerVal: bv,
				Diff:      diff,
			})
			report.BlockingReasons = append(report.BlockingReasons,
				fmt.Sprintf("cash_mismatch(%s): local=%d broker=%d diff=%d", ccy, lv, bv, diff))
		}
	}

	// 6. Reconcile positions across all symbols.
	localPos := engine.Ledger().PositionsForAccount(accountID)
	symbols := make(map[string]bool)
	for s := range localPos {
		symbols[s] = true
	}
	for s := range snap.Positions {
		symbols[s] = true
	}
	symList := make([]string, 0, len(symbols))
	for s := range symbols {
		symList = append(symList, s)
	}
	sort.Strings(symList)
	for _, sym := range symList {
		lv := localPos[sym]
		bv := snap.Positions[sym]
		diff := lv - bv
		if int64(math.Abs(float64(diff))) > policy.Reconciliation.MaxPositionToleranceQty {
			report.BalanceDiffs = append(report.BalanceDiffs, BalanceDiff{
				Kind:      "POSITION",
				Key:       sym,
				LocalVal:  lv,
				BrokerVal: bv,
				Diff:      diff,
			})
			report.BlockingReasons = append(report.BlockingReasons,
				fmt.Sprintf("position_mismatch(%s): local=%d broker=%d diff=%d", sym, lv, bv, diff))
		}
	}

	// 7. Verify no uncertain orders remain and ledger journal invariant holds.
	if rem := engine.OMS().UncertainOrders(); len(rem) > 0 {
		report.BlockingReasons = append(report.BlockingReasons, fmt.Sprintf("unresolved_uncertain_orders: %d", len(rem)))
	}
	if err := engine.Ledger().VerifyJournalInvariant(); err != nil {
		report.BlockingReasons = append(report.BlockingReasons, fmt.Sprintf("ledger_journal_invariant_failed: %v", err))
	}

	if len(report.BlockingReasons) > 0 {
		engine.IncrementMetric(func(m *domain.HealthMetrics) {
			m.ReconciliationFailures++
		})
		return report, domain.NewError(domain.ErrReconciliationMismatch, "reconcile",
			fmt.Sprintf("broker reconciliation failed with %d blocking reason(s)", len(report.BlockingReasons)),
			true, false, nil, map[string]any{"blocking_reasons": report.BlockingReasons})
	}

	report.Passed = true
	engine.IncrementMetric(func(m *domain.HealthMetrics) {
		m.ReconciliationSuccesses++
	})
	return report, nil
}
