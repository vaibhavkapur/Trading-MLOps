.PHONY: setup build test vet demo drills validate clean

BINARY := bin/drctl

setup:
	./scripts/setup.sh

build:
	mkdir -p bin
	go build -o $(BINARY) ./src/cmd/drctl

vet:
	go vet ./...

test:
	go test -v -race ./...

demo: build
	./scripts/demo.sh

drills: build
	./$(BINARY) drills --policy config/dr_policy.v1.json --failure-modes config/failure_modes.v1.json --fixtures data/fixtures/baseline_events.jsonl --out-json reports/drill_results.json --out-md reports/drill_report.md

validate:
	./scripts/validate.sh

clean:
	rm -rf bin /tmp/trading-dr-*
