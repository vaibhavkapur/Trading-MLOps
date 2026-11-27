package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/dr"
	"trading-dr-platform/src/fencing"
	"trading-dr-platform/src/reconcile"
	"trading-dr-platform/src/storage"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "demo":
		runDemo(os.Args[2:])
	case "drills":
		runDrills(os.Args[2:])
	case "verify-wal":
		runVerifyWAL(os.Args[2:])
	case "verify-backup":
		runVerifyBackup(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Println("drctl — Trading Disaster Recovery Platform CLI")
	fmt.Println("Usage:")
	fmt.Println("  drctl demo          [--policy ...] [--fixtures ...] [--broker ...]")
	fmt.Println("  drctl drills        [--policy ...] [--failure-modes ...] [--fixtures ...] [--broker ...] [--out-json ...] [--out-md ...]")
	fmt.Println("  drctl verify-wal    --wal <path>")
	fmt.Println("  drctl verify-backup --store <dir> --backup-id <id>")
}

func runDemo(args []string) {
	fs := flag.NewFlagSet("demo", flag.ExitOnError)
	policyPath := fs.String("policy", "config/dr_policy.v1.json", "path to DR policy JSON")
	fixturesPath := fs.String("fixtures", "data/fixtures/baseline_events.jsonl", "path to baseline events JSONL")
	brokerPath := fs.String("broker", "data/fixtures/broker_baseline.json", "path to broker baseline JSON")
	_ = fs.Parse(args)

	policy, err := domain.LoadPolicy(*policyPath)
	must(err)
	events, err := storage.LoadFixtureEvents(*fixturesPath)
	must(err)
	broker, err := reconcile.LoadSimulatedBrokerFromFile(*brokerPath)
	must(err)

	workDir, err := os.MkdirTemp("", "trading-dr-demo-*")
	must(err)
	defer os.RemoveAll(workDir)

	wal, err := storage.OpenWAL(filepath.Join(workDir, "wal", "events.wal.jsonl"))
	must(err)
	store, err := storage.NewLocalObjectStore(filepath.Join(workDir, "object_store"))
	must(err)
	bmgr := storage.NewBackupManager(store)
	fence, err := fencing.NewFileCoordinator(filepath.Join(workDir, "coord", "lease.json"))
	must(err)

	t0 := time.Date(2026, 1, 15, 14, 30, 5, 500000000, time.UTC)
	fmt.Println("=== Trading Disaster Recovery Platform — Reproducible Demo ===")
	fmt.Printf("Policy: %s (live_execution_allowed=%v, paper_sandbox_allowed=%v)\n",
		policy.PolicyID, policy.Execution.LiveExecutionAllowed, policy.Execution.PaperSandboxAllowed)

	primary := dr.NewNode("primary-east-1", policy, wal, bmgr, fence, broker)
	lease1, err := primary.AcquirePrimary(t0)
	must(err)
	fmt.Printf("[1] Primary acquired lease: holder=%s epoch=%d\n", lease1.HolderID, lease1.Epoch)

	for i := 0; i < 8; i++ {
		must(primary.PersistAndApply(events[i], t0))
	}
	manifest, err := primary.CreateCheckpointBackup("bkp-demo-0008", "chk-demo-0008", t0)
	must(err)
	fmt.Printf("[2] Created verified checkpoint backup: id=%s seq=%d sha256=%s\n",
		manifest.BackupID, manifest.LastSequence, manifest.PayloadSHA256[:16])

	for i := 8; i < len(events); i++ {
		must(primary.PersistAndApply(events[i], t0))
	}
	// Inject crash mid-submit order ORD-DEMO-CRASH that filled at broker before primary crashed.
	tCrash := t0.Add(200 * time.Millisecond)
	must(primary.PersistAndApply(domain.TradingEvent{
		EventID: "evt-demo-crash", Sequence: 14, EventType: domain.EventOrderIntent,
		EventTimeUTC: tCrash, AvailableTimeUTC: tCrash, AccountID: "ACCT-PAPER-01",
		ClientOrderID: "ORD-DEMO-CRASH", Symbol: "AAPL", Side: domain.SideBuy, Quantity: 10,
		LimitPriceMinor: 18600, Currency: "USD", IdempotencyKey: "idem-demo-crash", Source: "oms",
	}, tCrash))
	broker.InjectOrderAndFill(
		reconcile.BrokerOrder{
			ClientOrderID: "ORD-DEMO-CRASH", BrokerOrderID: "BRK-DEMO-1", Symbol: "AAPL",
			Side: domain.SideBuy, Quantity: 10, Currency: "USD", Status: domain.OrderFilled,
		},
		&domain.FillRecord{
			FillID: "FILL-DEMO-1", ClientOrderID: "ORD-DEMO-CRASH", BrokerOrderID: "BRK-DEMO-1",
			AccountID: "ACCT-PAPER-01", Symbol: "AAPL", Side: domain.SideBuy,
			Quantity: 10, PriceMinor: 18600, FeeMinor: 25, Currency: "USD", ExecutedAtUTC: tCrash,
		},
	)
	fmt.Println("[3] Simulated primary process crash mid-submit on ORD-DEMO-CRASH (lease expires)")

	// Standby promotes after lease TTL expires and fresh market tick arrives.
	tFailover := t0.Add(time.Duration(policy.Fencing.LeaseTTLMS+1000) * time.Millisecond)
	_, err = wal.Append(domain.TradingEvent{
		EventID: "evt-demo-mkt-fresh", Sequence: 15, EventType: domain.EventMarketSnapshot,
		EventTimeUTC: tFailover, AvailableTimeUTC: tFailover, Symbol: "AAPL",
		PriceMinor: 18600, Currency: "USD", IdempotencyKey: "mkt-demo-fresh", Source: "synthetic-feed",
	}, tFailover)
	must(err)
	broker.SetAsOfUTC(tFailover)

	standby := dr.NewNode("standby-west-2", policy, wal, bmgr, fence, broker)
	res, err := standby.RecoverAndFailover(dr.RecoveryRequest{
		AccountID:          "ACCT-PAPER-01",
		BackupID:           manifest.BackupID,
		WALPath:            wal.Path(),
		CrashRecovery:      true,
		AcquirePrimary:     true,
		PreFailureLastTime: tFailover,
		NowUTC:             tFailover,
	})
	must(err)

	fmt.Printf("[4] Standby failover complete: holder=%s epoch=%d measured_rpo_ms=%d measured_rto_ms=%.3f\n",
		res.Lease.HolderID, res.Lease.Epoch, res.MeasuredRPOMS, res.MeasuredRTOMS)
	fmt.Printf("[5] Reconciled balances: USD_minor=%d AAPL=%d MSFT=%d (gate_allowed=%v mode=%s)\n",
		standby.Engine.Ledger().CashBalance("ACCT-PAPER-01", "USD"),
		standby.Engine.Ledger().Position("ACCT-PAPER-01", "AAPL"),
		standby.Engine.Ledger().Position("ACCT-PAPER-01", "MSFT"),
		res.GateStatus.Allowed, res.GateStatus.Mode)

	// Prove stale primary is fenced.
	_, fenceErr := primary.SubmitOrder(domain.TradingEvent{
		EventID: "evt-stale-demo", Sequence: 16, EventType: domain.EventOrderIntent,
		EventTimeUTC: tFailover, AvailableTimeUTC: tFailover, AccountID: "ACCT-PAPER-01",
		ClientOrderID: "ORD-STALE-DEMO", Symbol: "AAPL", Side: domain.SideBuy, Quantity: 5,
		LimitPriceMinor: 18600, Currency: "USD", IdempotencyKey: "idem-stale-demo",
	}, tFailover)
	fmt.Printf("[6] Stale primary submission fenced: %v\n", fenceErr != nil)
}

func runDrills(args []string) {
	fs := flag.NewFlagSet("drills", flag.ExitOnError)
	policyPath := fs.String("policy", "config/dr_policy.v1.json", "path to DR policy JSON")
	fmPath := fs.String("failure-modes", "config/failure_modes.v1.json", "path to failure modes JSON")
	fixturesPath := fs.String("fixtures", "data/fixtures/baseline_events.jsonl", "path to baseline events JSONL")
	brokerPath := fs.String("broker", "data/fixtures/broker_baseline.json", "path to broker baseline JSON")
	outJSON := fs.String("out-json", "reports/drill_results.json", "output JSON evidence path")
	outMD := fs.String("out-md", "reports/drill_report.md", "output Markdown report path")
	_ = fs.Parse(args)

	policy, err := domain.LoadPolicy(*policyPath)
	must(err)
	inv, err := domain.LoadFailureModes(*fmPath)
	must(err)

	report, err := dr.RunAllDrills(policy, inv, *fixturesPath, *brokerPath)
	must(err)

	if *outJSON != "" {
		must(os.MkdirAll(filepath.Dir(*outJSON), 0o755))
		raw, err := json.MarshalIndent(report, "", "  ")
		must(err)
		must(os.WriteFile(*outJSON, raw, 0o644))
	}
	if *outMD != "" {
		must(os.MkdirAll(filepath.Dir(*outMD), 0o755))
		md := dr.FormatMarkdownReport(report)
		must(os.WriteFile(*outMD, []byte(md), 0o644))
	}

	passedCount := 0
	for _, s := range report.Scenarios {
		status := "PASS"
		if s.Passed {
			passedCount++
		} else {
			status = "FAIL"
		}
		fmt.Printf("[%s] %s %-26s RPO=%dms (target<=%dms) RTO=%.3fms (target<=%dms)\n",
			status, s.FailureModeID, s.ScenarioKey, s.MeasuredRPOMS, s.ProposedRPOMS, s.MeasuredRTOMS, s.ProposedRTOMS)
	}
	fmt.Printf("Drills complete: %d/%d passed (all_passed=%v)\n", passedCount, len(report.Scenarios), report.AllPassed)
	if !report.AllPassed {
		os.Exit(1)
	}
}

func runVerifyWAL(args []string) {
	fs := flag.NewFlagSet("verify-wal", flag.ExitOnError)
	walPath := fs.String("wal", "", "path to WAL JSONL file")
	_ = fs.Parse(args)
	if *walPath == "" {
		fmt.Fprintln(os.Stderr, "--wal is required")
		os.Exit(2)
	}
	recs, err := storage.ReadWALFile(*walPath, true)
	must(err)
	lastHash := storage.GenesisHash
	if len(recs) > 0 {
		lastHash = recs[len(recs)-1].Hash
	}
	fmt.Printf("WAL verified OK: records=%d last_hash=%s\n", len(recs), lastHash)
}

func runVerifyBackup(args []string) {
	fs := flag.NewFlagSet("verify-backup", flag.ExitOnError)
	storeDir := fs.String("store", "", "path to local object store directory")
	backupID := fs.String("backup-id", "", "backup ID to verify")
	_ = fs.Parse(args)
	if *storeDir == "" || *backupID == "" {
		fmt.Fprintln(os.Stderr, "--store and --backup-id are required")
		os.Exit(2)
	}
	store, err := storage.NewLocalObjectStore(*storeDir)
	must(err)
	bmgr := storage.NewBackupManager(store)
	state, manifest, err := bmgr.RestoreBackup(*backupID, true)
	must(err)
	fmt.Printf("Backup verified OK: backup_id=%s checkpoint_id=%s last_sequence=%d sha256=%s\n",
		manifest.BackupID, state.CheckpointID, state.LastSequence, manifest.PayloadSHA256)
}

func must(err error) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}
