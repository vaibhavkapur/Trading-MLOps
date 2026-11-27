package storage

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"trading-dr-platform/src/domain"
)

// ObjectStore abstracts durable backup/checkpoint object storage (e.g., S3, MinIO, GCS, or local filesystem).
type ObjectStore interface {
	Put(key string, data []byte) error
	Get(key string) ([]byte, error)
	List(prefix string) ([]string, error)
}

// LocalObjectStore stores immutable backup objects atomically on local disk via temp-file + fsync + rename.
type LocalObjectStore struct {
	root string
}

// NewLocalObjectStore creates a local filesystem ObjectStore rooted at dir.
func NewLocalObjectStore(dir string) (*LocalObjectStore, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, domain.NewError(domain.ErrInvalidInput, "storage", "failed creating object store root", true, false, err, map[string]any{"dir": dir})
	}
	return &LocalObjectStore{root: dir}, nil
}

// Root returns the root directory path.
func (s *LocalObjectStore) Root() string {
	return s.root
}

func (s *LocalObjectStore) fullPath(key string) (string, error) {
	clean := filepath.Clean(key)
	if strings.HasPrefix(clean, "..") || filepath.IsAbs(clean) {
		return "", domain.NewError(domain.ErrInvalidInput, "storage", "invalid object key", true, false, nil, map[string]any{"key": key})
	}
	return filepath.Join(s.root, clean), nil
}

// Put writes data atomically to key.
func (s *LocalObjectStore) Put(key string, data []byte) error {
	target, err := s.fullPath(key)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	tmp := target + ".tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, target)
}

// Get retrieves the raw bytes stored at key.
func (s *LocalObjectStore) Get(key string) ([]byte, error) {
	target, err := s.fullPath(key)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(target)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, domain.NewError(domain.ErrNotFound, "storage", fmt.Sprintf("object %q not found", key), true, false, err, map[string]any{"key": key})
		}
		return nil, err
	}
	return data, nil
}

// List returns sorted keys matching prefix.
func (s *LocalObjectStore) List(prefix string) ([]string, error) {
	keys := make([]string, 0)
	err := filepath.Walk(s.root, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(s.root, p)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if strings.HasPrefix(rel, prefix) {
			keys = append(keys, rel)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Strings(keys)
	return keys, nil
}

// BackupManifest records checksums, metadata, and provenance for a durable backup archive.
type BackupManifest struct {
	SchemaVersion string    `json:"schema_version"`
	BackupID      string    `json:"backup_id"`
	CheckpointID  string    `json:"checkpoint_id"`
	CreatedAtUTC  time.Time `json:"created_at_utc"`
	LastSequence  uint64    `json:"last_sequence"`
	LastWALHash   string    `json:"last_wal_hash"`
	PayloadKey    string    `json:"payload_key"`
	PayloadSHA256 string    `json:"payload_sha256"`
	PayloadBytes  int       `json:"payload_bytes"`
	Compression   string    `json:"compression"`
}

// BackupManager writes and restores SHA-256 verified checkpoint backups via ObjectStore.
type BackupManager struct {
	store ObjectStore
}

// NewBackupManager creates a BackupManager over store.
func NewBackupManager(store ObjectStore) *BackupManager {
	return &BackupManager{store: store}
}

// CreateBackup serializes, compresses, hashes, and persists a CheckpointState atomically.
func (m *BackupManager) CreateBackup(backupID string, state domain.CheckpointState) (BackupManifest, error) {
	if strings.TrimSpace(backupID) == "" {
		return BackupManifest{}, domain.NewError(domain.ErrInvalidInput, "backup", "backup_id is required", true, false, nil, nil)
	}
	rawJSON, err := json.Marshal(state)
	if err != nil {
		return BackupManifest{}, domain.NewError(domain.ErrInvalidInput, "backup", "failed marshaling checkpoint state", true, false, err, nil)
	}

	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	if _, err := gz.Write(rawJSON); err != nil {
		return BackupManifest{}, domain.NewError(domain.ErrBackupCorrupted, "backup", "failed compressing backup payload", true, false, err, nil)
	}
	if err := gz.Close(); err != nil {
		return BackupManifest{}, domain.NewError(domain.ErrBackupCorrupted, "backup", "failed finalizing gzip stream", true, false, err, nil)
	}
	payload := buf.Bytes()
	digest := sha256.Sum256(payload)
	hexDigest := hex.EncodeToString(digest[:])

	payloadKey := fmt.Sprintf("backups/%s/checkpoint.json.gz", backupID)
	manifestKey := fmt.Sprintf("backups/%s/manifest.json", backupID)

	manifest := BackupManifest{
		SchemaVersion: "1.0.0",
		BackupID:      backupID,
		CheckpointID:  state.CheckpointID,
		CreatedAtUTC:  state.CreatedAtUTC.UTC(),
		LastSequence:  state.LastSequence,
		LastWALHash:   state.LastWALHash,
		PayloadKey:    payloadKey,
		PayloadSHA256: hexDigest,
		PayloadBytes:  len(payload),
		Compression:   "gzip",
	}
	manifestBytes, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return BackupManifest{}, err
	}

	if err := m.store.Put(payloadKey, payload); err != nil {
		return BackupManifest{}, domain.NewError(domain.ErrBackupCorrupted, "backup", "failed writing backup payload", true, false, err, nil)
	}
	if err := m.store.Put(manifestKey, manifestBytes); err != nil {
		return BackupManifest{}, domain.NewError(domain.ErrBackupCorrupted, "backup", "failed writing backup manifest", true, false, err, nil)
	}
	return manifest, nil
}

// RestoreBackup loads a backup manifest and payload, verifies SHA-256 integrity, and decodes the CheckpointState.
func (m *BackupManager) RestoreBackup(backupID string, verifyChecksum bool) (domain.CheckpointState, BackupManifest, error) {
	manifestKey := fmt.Sprintf("backups/%s/manifest.json", backupID)
	manifestBytes, err := m.store.Get(manifestKey)
	if err != nil {
		return domain.CheckpointState{}, BackupManifest{}, domain.NewError(domain.ErrBackupCorrupted, "backup",
			fmt.Sprintf("failed loading manifest for backup %s", backupID), true, false, err, map[string]any{"backup_id": backupID})
	}
	var manifest BackupManifest
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
		return domain.CheckpointState{}, BackupManifest{}, domain.NewError(domain.ErrBackupCorrupted, "backup",
			fmt.Sprintf("corrupted manifest JSON for backup %s", backupID), true, false, err, map[string]any{"backup_id": backupID})
	}

	payload, err := m.store.Get(manifest.PayloadKey)
	if err != nil {
		return domain.CheckpointState{}, manifest, domain.NewError(domain.ErrBackupCorrupted, "backup",
			fmt.Sprintf("missing payload for backup %s", backupID), true, false, err, map[string]any{"backup_id": backupID})
	}

	if verifyChecksum {
		digest := sha256.Sum256(payload)
		gotHex := hex.EncodeToString(digest[:])
		if gotHex != manifest.PayloadSHA256 {
			return domain.CheckpointState{}, manifest, domain.NewError(domain.ErrBackupCorrupted, "backup",
				fmt.Sprintf("SHA-256 mismatch for backup %s: expected %s got %s", backupID, manifest.PayloadSHA256, gotHex),
				true, false, nil, map[string]any{
					"backup_id":       backupID,
					"expected_sha256": manifest.PayloadSHA256,
					"actual_sha256":   gotHex,
				})
		}
		if len(payload) != manifest.PayloadBytes {
			return domain.CheckpointState{}, manifest, domain.NewError(domain.ErrBackupCorrupted, "backup",
				fmt.Sprintf("payload byte length mismatch for backup %s: expected %d got %d", backupID, manifest.PayloadBytes, len(payload)),
				true, false, nil, map[string]any{"backup_id": backupID})
		}
	}

	gz, err := gzip.NewReader(bytes.NewReader(payload))
	if err != nil {
		return domain.CheckpointState{}, manifest, domain.NewError(domain.ErrBackupCorrupted, "backup",
			fmt.Sprintf("corrupted gzip header in backup %s", backupID), true, false, err, map[string]any{"backup_id": backupID})
	}
	defer gz.Close()

	rawJSON, err := io.ReadAll(gz)
	if err != nil {
		return domain.CheckpointState{}, manifest, domain.NewError(domain.ErrBackupCorrupted, "backup",
			fmt.Sprintf("corrupted gzip payload in backup %s", backupID), true, false, err, map[string]any{"backup_id": backupID})
	}

	var state domain.CheckpointState
	if err := json.Unmarshal(rawJSON, &state); err != nil {
		return domain.CheckpointState{}, manifest, domain.NewError(domain.ErrBackupCorrupted, "backup",
			fmt.Sprintf("corrupted checkpoint JSON in backup %s", backupID), true, false, err, map[string]any{"backup_id": backupID})
	}
	return state, manifest, nil
}
