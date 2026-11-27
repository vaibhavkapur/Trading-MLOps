package scenarios

import (
	"path/filepath"
	"testing"

	"trading-dr-platform/src/domain"
	"trading-dr-platform/src/dr"
)

func TestAllEightRequiredDisasterRecoveryScenarios(t *testing.T) {
	policyPath := filepath.Join("..", "..", "config", "dr_policy.v1.json")
	fmPath := filepath.Join("..", "..", "config", "failure_modes.v1.json")
	fixturePath := filepath.Join("..", "..", "data", "fixtures", "baseline_events.jsonl")
	brokerPath := filepath.Join("..", "..", "data", "fixtures", "broker_baseline.json")

	policy, err := domain.LoadPolicy(policyPath)
	if err != nil {
		t.Fatalf("load policy: %v", err)
	}
	inv, err := domain.LoadFailureModes(fmPath)
	if err != nil {
		t.Fatalf("load failure modes: %v", err)
	}

	report, err := dr.RunAllDrills(policy, inv, fixturePath, brokerPath)
	if err != nil {
		t.Fatalf("run drills failed: %v", err)
	}

	required := []string{
		"process_crash_mid_submit",
		"duplicate_events",
		"database_restore",
		"network_partition",
		"split_brain",
		"stale_snapshot",
		"backup_corruption",
		"broker_outage",
	}
	byKey := make(map[string]dr.ScenarioEvidence, len(report.Scenarios))
	for _, s := range report.Scenarios {
		byKey[s.ScenarioKey] = s
	}

	for _, key := range required {
		t.Run(key, func(t *testing.T) {
			ev, ok := byKey[key]
			if !ok {
				t.Fatalf("scenario %s missing from drill suite", key)
			}
			if !ev.Passed {
				t.Fatalf("scenario %s failed: notes=%s assertions=%v", key, ev.Notes, ev.Assertions)
			}
			if ev.MeasuredRPOMS > ev.ProposedRPOMS {
				t.Fatalf("scenario %s exceeded proposed RPO: measured=%dms proposed=%dms",
					key, ev.MeasuredRPOMS, ev.ProposedRPOMS)
			}
			if ev.MeasuredRTOMS > float64(ev.ProposedRTOMS) {
				t.Fatalf("scenario %s exceeded proposed RTO: measured=%.3fms proposed=%dms",
					key, ev.MeasuredRTOMS, ev.ProposedRTOMS)
			}
			if !ev.FailClosedProven {
				t.Fatalf("scenario %s did not prove fail-closed safety invariant", key)
			}
		})
	}

	if !report.AllPassed {
		t.Fatalf("expected all_passed=true across all 8 scenarios")
	}
}
