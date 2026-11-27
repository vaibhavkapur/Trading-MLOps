package domain

import (
	"errors"
	"fmt"
)

// ErrorCode classifies structured disaster recovery errors.
type ErrorCode string

const (
	ErrInvalidInput           ErrorCode = "INVALID_INPUT"
	ErrPolicyViolation        ErrorCode = "POLICY_VIOLATION"
	ErrBackupCorrupted        ErrorCode = "BACKUP_CORRUPTED"
	ErrWALCorrupted           ErrorCode = "WAL_CORRUPTED"
	ErrDuplicateEvent         ErrorCode = "DUPLICATE_EVENT"
	ErrFencingViolation       ErrorCode = "FENCING_VIOLATION"
	ErrSplitBrainDetected     ErrorCode = "SPLIT_BRAIN_DETECTED"
	ErrStaleSnapshot          ErrorCode = "STALE_SNAPSHOT"
	ErrBrokerUnavailable      ErrorCode = "BROKER_UNAVAILABLE"
	ErrUnknownExternalState   ErrorCode = "UNKNOWN_EXTERNAL_STATE"
	ErrReconciliationMismatch ErrorCode = "RECONCILIATION_MISMATCH"
	ErrExecutionBlocked       ErrorCode = "EXECUTION_BLOCKED"
	ErrOrderTransitionInvalid ErrorCode = "ORDER_TRANSITION_INVALID"
	ErrSequenceViolation      ErrorCode = "SEQUENCE_VIOLATION"
	ErrNotFound               ErrorCode = "NOT_FOUND"
)

// DRError is a structured, machine-inspectable error emitted across DR components.
type DRError struct {
	Code       ErrorCode      `json:"code"`
	Component  string         `json:"component"`
	Message    string         `json:"message"`
	Retryable  bool           `json:"retryable"`
	FailClosed bool           `json:"fail_closed"`
	Details    map[string]any `json:"details,omitempty"`
	Cause      error          `json:"-"`
}

func (e *DRError) Error() string {
	if e.Cause != nil {
		return fmt.Sprintf("[%s:%s] %s: %v", e.Code, e.Component, e.Message, e.Cause)
	}
	return fmt.Sprintf("[%s:%s] %s", e.Code, e.Component, e.Message)
}

func (e *DRError) Unwrap() error {
	return e.Cause
}

// NewError creates a structured DRError.
func NewError(code ErrorCode, component, message string, failClosed, retryable bool, cause error, details map[string]any) *DRError {
	return &DRError{
		Code:       code,
		Component:  component,
		Message:    message,
		Retryable:  retryable,
		FailClosed: failClosed,
		Details:    details,
		Cause:      cause,
	}
}

// HasCode checks whether err or any wrapped error is a *DRError with the given code.
func HasCode(err error, code ErrorCode) bool {
	var drErr *DRError
	if errors.As(err, &drErr) {
		return drErr.Code == code
	}
	return false
}
