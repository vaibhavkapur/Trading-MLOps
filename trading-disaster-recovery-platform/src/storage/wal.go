package storage

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"trading-dr-platform/src/domain"
)

const GenesisHash = "GENESIS"

// WALRecord wraps a TradingEvent with tamper-evident SHA-256 hash chaining.
type WALRecord struct {
	RecordIndex   uint64              `json:"record_index"`
	PrevHash      string              `json:"prev_hash"`
	Hash          string              `json:"hash"`
	RecordedAtUTC time.Time           `json:"recorded_at_utc"`
	Event         domain.TradingEvent `json:"event"`
}

// ComputeRecordHash computes the deterministic SHA-256 chain hash for a WAL record.
func ComputeRecordHash(index uint64, prevHash string, evt domain.TradingEvent) (string, error) {
	payload, err := json.Marshal(evt)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	_, _ = fmt.Fprintf(h, "%d|%s|", index, prevHash)
	_, _ = h.Write(payload)
	return hex.EncodeToString(h.Sum(nil)), nil
}

// WAL is an append-only durable JSONL write-ahead log with fsync and hash-chain verification.
type WAL struct {
	mu          sync.Mutex
	path        string
	recordCount uint64
	lastHash    string
}

// OpenWAL opens (or initializes) a WAL file at path and verifies any existing chain.
func OpenWAL(path string) (*WAL, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, domain.NewError(domain.ErrWALCorrupted, "storage", "failed creating WAL directory", true, false, err, map[string]any{"path": path})
	}
	w := &WAL{
		path:     path,
		lastHash: GenesisHash,
	}
	if _, err := os.Stat(path); err == nil {
		recs, err := w.ReadAll(true)
		if err != nil {
			return nil, err
		}
		if len(recs) > 0 {
			last := recs[len(recs)-1]
			w.recordCount = last.RecordIndex
			w.lastHash = last.Hash
		}
	}
	return w, nil
}

// Path returns the WAL file path.
func (w *WAL) Path() string {
	return w.path
}

// LastHash returns the latest hash in the WAL chain.
func (w *WAL) LastHash() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.lastHash
}

// Append validates, hash-chains, writes, and fsyncs a TradingEvent to the WAL.
func (w *WAL) Append(evt domain.TradingEvent, now time.Time) (WALRecord, error) {
	if err := evt.Validate(); err != nil {
		return WALRecord{}, err
	}
	w.mu.Lock()
	defer w.mu.Unlock()

	idx := w.recordCount + 1
	hash, err := ComputeRecordHash(idx, w.lastHash, evt)
	if err != nil {
		return WALRecord{}, domain.NewError(domain.ErrWALCorrupted, "storage", "failed hashing WAL event", true, false, err, nil)
	}
	rec := WALRecord{
		RecordIndex:   idx,
		PrevHash:      w.lastHash,
		Hash:          hash,
		RecordedAtUTC: now.UTC(),
		Event:         evt,
	}
	line, err := json.Marshal(rec)
	if err != nil {
		return WALRecord{}, domain.NewError(domain.ErrWALCorrupted, "storage", "failed encoding WAL record", true, false, err, nil)
	}

	f, err := os.OpenFile(w.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return WALRecord{}, domain.NewError(domain.ErrWALCorrupted, "storage", "failed opening WAL for append", true, false, err, map[string]any{"path": w.path})
	}
	_, writeErr := f.Write(append(line, '\n'))
	syncErr := f.Sync()
	closeErr := f.Close()
	if writeErr != nil {
		return WALRecord{}, domain.NewError(domain.ErrWALCorrupted, "storage", "failed writing WAL record", true, false, writeErr, nil)
	}
	if syncErr != nil {
		return WALRecord{}, domain.NewError(domain.ErrWALCorrupted, "storage", "failed fsyncing WAL record", true, false, syncErr, nil)
	}
	if closeErr != nil {
		return WALRecord{}, domain.NewError(domain.ErrWALCorrupted, "storage", "failed closing WAL file", true, false, closeErr, nil)
	}

	w.recordCount = idx
	w.lastHash = hash
	return rec, nil
}

// ReadAll reads every record from the WAL and optionally validates the SHA-256 hash chain.
func (w *WAL) ReadAll(verifyChain bool) ([]WALRecord, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return ReadWALFile(w.path, verifyChain)
}

// ReadWALFile reads and verifies a WAL file directly from disk.
func ReadWALFile(path string, verifyChain bool) ([]WALRecord, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, domain.NewError(domain.ErrWALCorrupted, "storage", "failed opening WAL file", true, false, err, map[string]any{"path": path})
	}
	defer f.Close()

	records := make([]WALRecord, 0)
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	prevHash := GenesisHash
	var expectedIdx uint64 = 1

	lineNo := 0
	for scanner.Scan() {
		lineNo++
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var rec WALRecord
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			return nil, domain.NewError(domain.ErrWALCorrupted, "storage",
				fmt.Sprintf("malformed WAL JSON at line %d", lineNo), true, false, err, map[string]any{"line": lineNo, "path": path})
		}
		if verifyChain {
			if rec.RecordIndex != expectedIdx {
				return nil, domain.NewError(domain.ErrWALCorrupted, "storage",
					fmt.Sprintf("WAL record index mismatch at line %d: got %d want %d", lineNo, rec.RecordIndex, expectedIdx),
					true, false, nil, map[string]any{"line": lineNo})
			}
			if rec.PrevHash != prevHash {
				return nil, domain.NewError(domain.ErrWALCorrupted, "storage",
					fmt.Sprintf("WAL hash-chain break at record %d: prev_hash mismatch", rec.RecordIndex),
					true, false, nil, map[string]any{"record_index": rec.RecordIndex})
			}
			computed, err := ComputeRecordHash(rec.RecordIndex, rec.PrevHash, rec.Event)
			if err != nil || computed != rec.Hash {
				return nil, domain.NewError(domain.ErrWALCorrupted, "storage",
					fmt.Sprintf("WAL payload digest mismatch at record %d", rec.RecordIndex),
					true, false, err, map[string]any{"record_index": rec.RecordIndex})
			}
		}
		prevHash = rec.Hash
		expectedIdx++
		records = append(records, rec)
	}
	if err := scanner.Err(); err != nil {
		return nil, domain.NewError(domain.ErrWALCorrupted, "storage", "error scanning WAL file", true, false, err, map[string]any{"path": path})
	}
	return records, nil
}

// LoadFixtureEvents loads newline-delimited TradingEvents from a fixture file.
func LoadFixtureEvents(path string) ([]domain.TradingEvent, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, domain.NewError(domain.ErrInvalidInput, "storage", "failed opening fixture file", true, false, err, map[string]any{"path": path})
	}
	defer f.Close()

	events := make([]domain.TradingEvent, 0)
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	lineNo := 0
	for scanner.Scan() {
		lineNo++
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var evt domain.TradingEvent
		if err := json.Unmarshal([]byte(line), &evt); err != nil {
			return nil, domain.NewError(domain.ErrInvalidInput, "storage", fmt.Sprintf("invalid fixture JSON at line %d", lineNo), true, false, err, nil)
		}
		if err := evt.Validate(); err != nil {
			return nil, err
		}
		events = append(events, evt)
	}
	if err := scanner.Err(); err != nil {
		return nil, domain.NewError(domain.ErrInvalidInput, "storage", "failed reading fixture file", true, false, err, nil)
	}
	return events, nil
}
