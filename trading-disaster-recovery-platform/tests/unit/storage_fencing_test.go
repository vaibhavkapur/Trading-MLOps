package unit

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/fencing"
	"trading-dr-platform/src/replay"
	"trading-dr-platform/src/storage"
)

func TestWALHashChainAndTamperDetection(t *testing.T) {
	dir := t.TempDir()
	walPath := filepath.Join(dir, "test.wal.jsonl")
	w, err := storage.OpenWAL(walPath)
	if err != nil {
		t.Fatalf("open WAL failed: %v", err)
	}

	t0 := time.Date(2026, 1, 15, 14, 30, 0, 0, time.UTC)
	evt1 := domain.TradingEvent{
		EventID: "e1", Sequence: 1, EventType: domain.EventCashDeposit,
		EventTimeUTC: t0, AvailableTimeUTC: t0, AccountID: "A1",
		Currency: "USD", AmountMinor: 50000, IdempotencyKey: "k1",
	}
	evt2 := domain.TradingEvent{
		EventID: "e2", Sequence: 2, EventType: domain.EventMarketSnapshot,
		EventTimeUTC: t0.Add(time.Second), AvailableTimeUTC: t0.Add(time.Second),
		Symbol: "AAPL", PriceMinor: 18500, Currency: "USD", IdempotencyKey: "k2",
	}

	if _, err := w.Append(evt1, t0); err != nil {
		t.Fatalf("append 1 failed: %v", err)
	}
	if _, err := w.Append(evt2, t0); err != nil {
		t.Fatalf("append 2 failed: %v", err)
	}

	recs, err := w.ReadAll(true)
	if err != nil || len(recs) != 2 {
		t.Fatalf("read WAL failed: len=%d err=%v", len(recs), err)
	}
	if recs[1].PrevHash != recs[0].Hash {
		t.Fatalf("expected chained prev_hash")
	}

	// Tamper with amount in file on disk -> must fail hash-chain verification.
	raw, _ := os.ReadFile(walPath)
	tampered := strings.Replace(string(raw), "50000", "99999", 1)
	_ = os.WriteFile(walPath, []byte(tampered), 0o644)

	if _, err := storage.ReadWALFile(walPath, true); !domain.HasCode(err, domain.ErrWALCorrupted) {
		t.Fatalf("expected ErrWALCorrupted on tampered WAL, got: %v", err)
	}
}

func TestBackupManagerChecksumIntegrity(t *testing.T) {
	dir := t.TempDir()
	store, err := storage.NewLocalObjectStore(filepath.Join(dir, "obj"))
	if err != nil {
		t.Fatalf("store init failed: %v", err)
	}
	mgr := storage.NewBackupManager(store)

	eng := replay.NewEngine()
	t0 := time.Date(2026, 1, 15, 14, 30, 0, 0, time.UTC)
	_, _, err = eng.ApplyEvent(domain.TradingEvent{
		EventID: "e1", Sequence: 1, EventType: domain.EventCashDeposit,
		EventTimeUTC: t0, AvailableTimeUTC: t0, AccountID: "A1",
		Currency: "USD", AmountMinor: 123456, IdempotencyKey: "k1",
	}, "hash1")
	if err != nil {
		t.Fatalf("apply event failed: %v", err)
	}

	cp := eng.Snapshot("chk-1", t0)
	manifest, err := mgr.CreateBackup("bkp-1", cp)
	if err != nil {
		t.Fatalf("create backup failed: %v", err)
	}

	restored, _, err := mgr.RestoreBackup("bkp-1", true)
	if err != nil {
		t.Fatalf("restore backup failed: %v", err)
	}
	if restored.Ledger.CashBalances["A1"]["USD"] != 123456 {
		t.Fatalf("restored cash mismatch: got %d", restored.Ledger.CashBalances["A1"]["USD"])
	}

	// Corrupt a byte in payload -> RestoreBackup must fail with ErrBackupCorrupted.
	payloadPath := filepath.Join(store.Root(), manifest.PayloadKey)
	raw, _ := os.ReadFile(payloadPath)
	raw[len(raw)-1] ^= 0xAA
	_ = os.WriteFile(payloadPath, raw, 0o644)

	if _, _, err := mgr.RestoreBackup("bkp-1", true); !domain.HasCode(err, domain.ErrBackupCorrupted) {
		t.Fatalf("expected ErrBackupCorrupted on corrupted payload, got: %v", err)
	}
}

func TestFencingCoordinatorMonotonicEpochsAndSplitBrain(t *testing.T) {
	dir := t.TempDir()
	coord, err := fencing.NewFileCoordinator(filepath.Join(dir, "lease.json"))
	if err != nil {
		t.Fatalf("coordinator init failed: %v", err)
	}
	t0 := time.Date(2026, 1, 15, 14, 30, 0, 0, time.UTC)
	ttl := 5 * time.Second

	l1, err := coord.Acquire("node-A", t0, ttl)
	if err != nil || l1.Epoch != 1 {
		t.Fatalf("acquire node-A failed: lease=%+v err=%v", l1, err)
	}

	// node-B cannot acquire while node-A's lease is active.
	if _, err := coord.Acquire("node-B", t0.Add(2*time.Second), ttl); !domain.HasCode(err, domain.ErrFencingViolation) {
		t.Fatalf("expected ErrFencingViolation while lease active, got: %v", err)
	}

	// After expiry, node-B acquires Epoch 2.
	tExp := t0.Add(6 * time.Second)
	l2, err := coord.Acquire("node-B", tExp, ttl)
	if err != nil || l2.Epoch != 2 {
		t.Fatalf("acquire node-B after expiry failed: lease=%+v err=%v", l2, err)
	}

	// Stale node-A validating or renewing Epoch 1 must be fenced with ErrSplitBrainDetected.
	if err := coord.ValidateFence("node-A", l1.Epoch, tExp); !domain.HasCode(err, domain.ErrSplitBrainDetected) {
		t.Fatalf("expected ErrSplitBrainDetected for stale epoch 1, got: %v", err)
	}
	if _, err := coord.Renew("node-A", l1.Epoch, tExp, ttl); !domain.HasCode(err, domain.ErrSplitBrainDetected) {
		t.Fatalf("expected ErrSplitBrainDetected on stale renew, got: %v", err)
	}

	// Partitioning node-B blocks validation immediately.
	coord.SetPartitioned("node-B", true)
	if err := coord.ValidateFence("node-B", l2.Epoch, tExp.Add(time.Second)); !domain.HasCode(err, domain.ErrFencingViolation) {
		t.Fatalf("expected ErrFencingViolation when partitioned, got: %v", err)
	}
}
