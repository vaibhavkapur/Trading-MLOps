package integration

import (
	"path/filepath"
	"testing"
	"time"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/dr"
	"trading-dr-platform/src/fencing"
	"trading-dr-platform/src/reconcile"
	"trading-dr-platform/src/storage"
)

func loadFixtures(t *testing.T) (domain.DRPolicy, []domain.TradingEvent, *reconcile.SimulatedBroker) {
	t.Helper()
	policy, err := domain.LoadPolicy(filepath.Join("..", "..", "config", "dr_policy.v1.json"))
	if err != nil {
		t.Fatalf("load policy: %v", err)
	}
	events, err := storage.LoadFixtureEvents(filepath.Join("..", "..", "data", "fixtures", "baseline_events.jsonl"))
	if err != nil {
		t.Fatalf("load events: %v", err)
	}
	broker, err := reconcile.LoadSimulatedBrokerFromFile(filepath.Join("..", "..", "data", "fixtures", "broker_baseline.json"))
	if err != nil {
		t.Fatalf("load broker baseline: %v", err)
	}
	return policy, events, broker
}

func TestEndToEndCheckpointRestoreReplayAndReconcile(t *testing.T) {
	policy, events, broker := loadFixtures(t)
	dir := t.TempDir()

	wal, err := storage.OpenWAL(filepath.Join(dir, "wal", "events.wal.jsonl"))
	if err != nil {
		t.Fatalf("open WAL: %v", err)
	}
	store, err := storage.NewLocalObjectStore(filepath.Join(dir, "obj"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	bmgr := storage.NewBackupManager(store)
	fence, err := fencing.NewFileCoordinator(filepath.Join(dir, "lease.json"))
	if err != nil {
		t.Fatalf("open fence: %v", err)
	}

	t0 := time.Date(2026, 1, 15, 14, 30, 5, 500000000, time.UTC)
	primary := dr.NewNode("primary", policy, wal, bmgr, fence, broker)
	lease, err := primary.AcquirePrimary(t0)
	if err != nil {
		t.Fatalf("acquire primary: %v", err)
	}
	for i := 0; i < 6; i++ {
		if err := primary.PersistAndApply(events[i], t0); err != nil {
			t.Fatalf("apply %d: %v", i, err)
		}
	}
	manifest, err := primary.CreateCheckpointBackup("bkp-6", "chk-6", t0)
	if err != nil {
		t.Fatalf("create backup: %v", err)
	}
	for i := 6; i < len(events); i++ {
		if err := primary.PersistAndApply(events[i], t0); err != nil {
			t.Fatalf("apply %d: %v", i, err)
		}
	}
	if err := fence.Release("primary", lease.Epoch); err != nil {
		t.Fatalf("release lease: %v", err)
	}

	tRec := t0.Add(200 * time.Millisecond)
	broker.SetAsOfUTC(tRec)
	standby := dr.NewNode("standby", policy, wal, bmgr, fence, broker)
	res, err := standby.RecoverAndFailover(dr.RecoveryRequest{
		AccountID:          "ACCT-PAPER-01",
		BackupID:           manifest.BackupID,
		WALPath:            wal.Path(),
		AcquirePrimary:     true,
		PreFailureLastTime: events[len(events)-1].EventTimeUTC,
		NowUTC:             tRec,
	})
	if err != nil {
		t.Fatalf("recover failed: %v", err)
	}
	if !res.GateStatus.Allowed || res.GateStatus.Mode != "PAPER_SANDBOX" {
		t.Fatalf("expected PAPER_SANDBOX gate allowed, got %+v", res.GateStatus)
	}
	if standby.Engine.Ledger().CashBalance("ACCT-PAPER-01", "USD") != 96793740 {
		t.Fatalf("unexpected cash balance: %d", standby.Engine.Ledger().CashBalance("ACCT-PAPER-01", "USD"))
	}
	if standby.Engine.Ledger().Position("ACCT-PAPER-01", "AAPL") != 60 || standby.Engine.Ledger().Position("ACCT-PAPER-01", "MSFT") != 50 {
		t.Fatalf("unexpected positions: AAPL=%d MSFT=%d",
			standby.Engine.Ledger().Position("ACCT-PAPER-01", "AAPL"),
			standby.Engine.Ledger().Position("ACCT-PAPER-01", "MSFT"))
	}
}

func TestReconciliationBlocksOnCashOrPositionDrift(t *testing.T) {
	policy, events, broker := loadFixtures(t)
	dir := t.TempDir()

	wal, _ := storage.OpenWAL(filepath.Join(dir, "wal.jsonl"))
	store, _ := storage.NewLocalObjectStore(filepath.Join(dir, "obj"))
	fence, _ := fencing.NewFileCoordinator(filepath.Join(dir, "lease.json"))
	t0 := time.Date(2026, 1, 15, 14, 30, 5, 500000000, time.UTC)
	for _, e := range events {
		_, _ = wal.Append(e, t0)
	}

	// Inject unexplained $5.00 broker cash drift.
	broker.InjectCashDelta("USD", -500)
	broker.SetAsOfUTC(t0)

	node := dr.NewNode("drift-node", policy, wal, storage.NewBackupManager(store), fence, broker)
	res, err := node.RecoverAndFailover(dr.RecoveryRequest{
		AccountID:      "ACCT-PAPER-01",
		WALPath:        wal.Path(),
		AcquirePrimary: true,
		NowUTC:         t0,
	})
	if !domain.HasCode(err, domain.ErrReconciliationMismatch) {
		t.Fatalf("expected ErrReconciliationMismatch on broker cash drift, got: %v", err)
	}
	if res.GateStatus.Allowed {
		t.Fatalf("execution gate must remain closed on reconciliation mismatch")
	}
	if res.ReconciliationReport == nil || len(res.ReconciliationReport.BalanceDiffs) != 1 {
		t.Fatalf("expected 1 balance diff recorded, got %+v", res.ReconciliationReport)
	}
}
