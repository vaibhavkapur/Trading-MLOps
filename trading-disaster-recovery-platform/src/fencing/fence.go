package fencing

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"trading-dr-platform/src/domain"
)

// Coordinator provides monotonic fencing tokens (epochs) and exclusive primary leases
// to prevent active-active split-brain order submission.
type Coordinator interface {
	Acquire(holderID string, now time.Time, ttl time.Duration) (domain.Lease, error)
	Renew(holderID string, epoch uint64, now time.Time, ttl time.Duration) (domain.Lease, error)
	Release(holderID string, epoch uint64) error
	ValidateFence(holderID string, epoch uint64, now time.Time) error
	CurrentLease() (domain.Lease, bool)
}

type leaseState struct {
	HighestEpoch uint64        `json:"highest_epoch"`
	Current      *domain.Lease `json:"current,omitempty"`
}

// FileCoordinator persists lease and monotonic epoch state atomically on disk
// and supports deterministic partition fault injection for testing.
type FileCoordinator struct {
	mu          sync.Mutex
	path        string
	state       leaseState
	partitioned map[string]bool
}

// NewFileCoordinator initializes or reloads a FileCoordinator at path.
func NewFileCoordinator(path string) (*FileCoordinator, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, domain.NewError(domain.ErrFencingViolation, "fencing", "failed creating lease state directory", true, false, err, nil)
	}
	fc := &FileCoordinator{
		path:        path,
		partitioned: make(map[string]bool),
	}
	if raw, err := os.ReadFile(path); err == nil && len(raw) > 0 {
		if err := json.Unmarshal(raw, &fc.state); err != nil {
			return nil, domain.NewError(domain.ErrFencingViolation, "fencing", "corrupted lease state file", true, false, err, map[string]any{"path": path})
		}
	}
	return fc, nil
}

func (c *FileCoordinator) persistLocked() error {
	raw, err := json.MarshalIndent(c.state, "", "  ")
	if err != nil {
		return err
	}
	tmp := c.path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(raw); err != nil {
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
	return os.Rename(tmp, c.path)
}

// SetPartitioned injects or clears a simulated network partition for a specific holder node.
func (c *FileCoordinator) SetPartitioned(holderID string, partitioned bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if partitioned {
		c.partitioned[holderID] = true
	} else {
		delete(c.partitioned, holderID)
	}
}

// Acquire grants an exclusive primary lease with a strictly increasing monotonic epoch
// if no active unexpired lease is held by another node.
func (c *FileCoordinator) Acquire(holderID string, now time.Time, ttl time.Duration) (domain.Lease, error) {
	if holderID == "" || ttl <= 0 {
		return domain.Lease{}, domain.NewError(domain.ErrInvalidInput, "fencing", "holderID and positive ttl required", true, false, nil, nil)
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.partitioned[holderID] {
		return domain.Lease{}, domain.NewError(domain.ErrFencingViolation, "fencing",
			fmt.Sprintf("holder %s is network-partitioned from lease coordinator", holderID), true, true, nil, map[string]any{"holder_id": holderID})
	}

	nowUTC := now.UTC()
	if c.state.Current != nil && c.state.Current.HolderID != holderID && nowUTC.Before(c.state.Current.ExpiresAtUTC) {
		return domain.Lease{}, domain.NewError(domain.ErrFencingViolation, "fencing",
			fmt.Sprintf("lease currently held by %s (epoch %d) until %s", c.state.Current.HolderID, c.state.Current.Epoch, c.state.Current.ExpiresAtUTC.Format(time.RFC3339Nano)),
			true, true, nil, map[string]any{
				"active_holder": c.state.Current.HolderID,
				"active_epoch":  c.state.Current.Epoch,
			})
	}

	c.state.HighestEpoch++
	lease := domain.Lease{
		HolderID:      holderID,
		Epoch:         c.state.HighestEpoch,
		AcquiredAtUTC: nowUTC,
		ExpiresAtUTC:  nowUTC.Add(ttl),
	}
	c.state.Current = &lease
	if err := c.persistLocked(); err != nil {
		return domain.Lease{}, domain.NewError(domain.ErrFencingViolation, "fencing", "failed persisting acquired lease", true, false, err, nil)
	}
	return lease, nil
}

// Renew extends an existing active lease if the caller still owns the highest epoch and is not partitioned.
func (c *FileCoordinator) Renew(holderID string, epoch uint64, now time.Time, ttl time.Duration) (domain.Lease, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.partitioned[holderID] {
		return domain.Lease{}, domain.NewError(domain.ErrFencingViolation, "fencing",
			fmt.Sprintf("holder %s cannot renew lease during network partition", holderID), true, true, nil, map[string]any{"holder_id": holderID})
	}
	if c.state.Current == nil {
		return domain.Lease{}, domain.NewError(domain.ErrFencingViolation, "fencing", "no active lease to renew", true, false, nil, nil)
	}
	if epoch < c.state.HighestEpoch || c.state.Current.HolderID != holderID || c.state.Current.Epoch != epoch {
		return domain.Lease{}, domain.NewError(domain.ErrSplitBrainDetected, "fencing",
			fmt.Sprintf("stale lease renewal rejected for %s epoch %d (current holder=%s epoch=%d)", holderID, epoch, c.state.Current.HolderID, c.state.HighestEpoch),
			true, false, nil, map[string]any{
				"caller_holder": holderID,
				"caller_epoch":  epoch,
				"active_holder": c.state.Current.HolderID,
				"active_epoch":  c.state.HighestEpoch,
			})
	}
	nowUTC := now.UTC()
	if !nowUTC.Before(c.state.Current.ExpiresAtUTC) {
		return domain.Lease{}, domain.NewError(domain.ErrFencingViolation, "fencing",
			fmt.Sprintf("lease for %s epoch %d already expired at %s", holderID, epoch, c.state.Current.ExpiresAtUTC.Format(time.RFC3339Nano)),
			true, false, nil, nil)
	}

	c.state.Current.ExpiresAtUTC = nowUTC.Add(ttl)
	if err := c.persistLocked(); err != nil {
		return domain.Lease{}, err
	}
	return *c.state.Current, nil
}

// Release voluntarily releases the active lease held by holderID at epoch.
func (c *FileCoordinator) Release(holderID string, epoch uint64) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.state.Current == nil || c.state.Current.HolderID != holderID || c.state.Current.Epoch != epoch {
		return domain.NewError(domain.ErrFencingViolation, "fencing", "cannot release unowned or stale lease", true, false, nil, nil)
	}
	c.state.Current = nil
	return c.persistLocked()
}

// ValidateFence verifies that holderID owns the active, unexpired, highest monotonic epoch.
// Rejects stale primaries with ErrSplitBrainDetected and partitioned/expired holders with ErrFencingViolation.
func (c *FileCoordinator) ValidateFence(holderID string, epoch uint64, now time.Time) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.partitioned[holderID] {
		return domain.NewError(domain.ErrFencingViolation, "fencing",
			fmt.Sprintf("holder %s cannot validate fence while network-partitioned", holderID), true, true, nil, map[string]any{"holder_id": holderID})
	}
	if epoch == 0 {
		return domain.NewError(domain.ErrFencingViolation, "fencing", "missing fencing epoch (epoch=0)", true, false, nil, nil)
	}
	if epoch < c.state.HighestEpoch {
		return domain.NewError(domain.ErrSplitBrainDetected, "fencing",
			fmt.Sprintf("split-brain fenced: caller %s presented stale epoch %d < highest epoch %d", holderID, epoch, c.state.HighestEpoch),
			true, false, nil, map[string]any{
				"caller_holder": holderID,
				"caller_epoch":  epoch,
				"highest_epoch": c.state.HighestEpoch,
			})
	}
	if c.state.Current == nil {
		return domain.NewError(domain.ErrFencingViolation, "fencing", "no active primary lease exists", true, false, nil, nil)
	}
	if c.state.Current.HolderID != holderID || c.state.Current.Epoch != epoch {
		return domain.NewError(domain.ErrSplitBrainDetected, "fencing",
			fmt.Sprintf("split-brain fenced: lease owned by %s (epoch %d), not %s (epoch %d)", c.state.Current.HolderID, c.state.Current.Epoch, holderID, epoch),
			true, false, nil, nil)
	}
	if !now.UTC().Before(c.state.Current.ExpiresAtUTC) {
		return domain.NewError(domain.ErrFencingViolation, "fencing",
			fmt.Sprintf("lease expired for %s epoch %d at %s", holderID, epoch, c.state.Current.ExpiresAtUTC.Format(time.RFC3339Nano)),
			true, false, nil, nil)
	}
	return nil
}

// CurrentLease returns the currently held lease if one exists.
func (c *FileCoordinator) CurrentLease() (domain.Lease, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.state.Current == nil {
		return domain.Lease{}, false
	}
	return *c.state.Current, true
}
