# Contracts and decision semantics

## Registry

Register the complete version-1 JSON object in `config/monitor.v1.json`. The registry identifier binds model name/version, feature names and units, reference interval, rolling width and step, minimum sample, horizon and grace, shift thresholds and cooldown. Rates and probabilities use fractions. Durations use minutes. IDs use ASCII letters, digits, periods, underscores and hyphens with an alphanumeric first character. Re-registering identical settings under the same reviewer is idempotent. Changed settings require a new registry ID.

Every public timestamp is UTC ISO 8601 with a literal Z, seconds and optional microseconds. Storage normalizes to six fractional digits. Event windows are half-open. Window ends fall on the step grid anchored at baseline end. A window must start at or after baseline end and close by its evaluation cutoff.

## Input stream

One JSON object per nonblank line, scoped by the CLI's required registry. Insert the observation before its label within or across batches. Use a stable identifier per forecast. The example matches the supplied registry.

```json
{"kind":"observation","id":"forecast-42","event_at":"2026-01-08T00:15:00Z","available_at":"2026-01-08T00:15:03Z","features":{"momentum":0.2,"spread":null},"probability":0.62}
{"kind":"label","id":"forecast-42","available_at":"2026-01-08T00:26:00Z","outcome":1}
```

Feature keys match the declared dictionary exactly. Each value is finite numeric or explicit null. Probability is a fraction in [0,1] or null. Outcome is the integer 0 or 1. Observation availability must follow or equal its event time. Label availability must follow or equal both observation availability and event time plus target horizon. Availability means first usable by the monitor. Record the actual arrival of delayed inputs.

A bad row aborts the whole batch. The same normalized JSON record can be replayed. Conflicting IDs and new records available at or before a registry's published evaluation watermark are rejected. This preserves the reproducibility of earlier cutoffs. Historical revisions require a separately registered replay. Ahead-of-clock inputs can be preloaded and become visible only at their declared availability.

## Evidence and thresholds

1. Select baseline observations in the reference interval available by baseline end, and current observations in the rolling interval available by cutoff. Join only labels visible at those respective cutoffs.
2. Partition by event-time UTC hour. Include each hour touched by the window even if empty. Below 40 observations on either side, record `inconclusive` for the stratum.
3. Measure per-series null fractions for every feature and prediction. A fraction at or above 15% on either side or a current increase of at least 8 percentage points opens a quality signal. Block distribution and performance conclusions for that stratum. Label coverage remains separately observable.
4. For complete values with at least 40 on both sides, flag a KS distance at or above 0.30. Lower usable counts produce an inconclusive result.
5. A forecast matures at event time plus horizon and grace. Count pending, mature, overdue, coverage and usable pairs. Expected labels with less than 90% coverage in a sufficiently large mature cohort open a label-quality ticket and block performance conclusions. Early labels wait until cohort maturity. Thin usable cohorts produce pending or inconclusive evidence.
6. From matched, mature, labeled forecasts compute accuracy at probability 0.5, mean squared probability error (Brier), and expected calibration error (ECE). ECE uses ten equal-width probability bins, assigns 1 to the last bin and weights the absolute mean probability/outcome gap by bin count. Trigger at accuracy drop ≥0.18, Brier increase ≥0.08, or ECE increase ≥0.18. Each comparison uses the unrounded metric.
7. Deduplicate the first actionable occurrence per registry, window end and signal. A new ticket for the same registry and signal waits 180 minutes from its last opening, measured on the evaluation cutoff clock. At cooldown expiry another window can open a ticket. Suppressed windows retain a decision pointing to the original ticket. Reviews have no effect on this clock.

## CLI and local interfaces

Run `bin/drift --help` or add `--help` after a subcommand. Select the database with `DRIFT_DB` or global `--db PATH`. The default is `var/monitor.sqlite3` relative to the caller.

- `register CONFIG [--reviewer-account ACCOUNT]` validates policy, binds a local reviewer and returns its configuration hash.
- `ingest FILE --registry ID` atomically imports NDJSON and reports inserted and duplicate counts. Supply `-` for stdin.
- `evaluate --registry ID --end UTC --as-of UTC` evaluates one scheduled window, returning JSON evidence. Exact retries return the original run. New evaluations require a nondecreasing cutoff.
- `tick --registry ID [--as-of UTC]` catches up from baseline end, rechecks affected older windows and returns committed run IDs. Without a cutoff it uses the current UTC time.
- `show RUN_ID` returns the immutable scores and policy decisions first made on that run. A revision with a later cutoff receives its own run ID.
- `review TICKET_ID acknowledged|resolved --note TEXT` records an attributed human decision. Allowed transitions are open to acknowledged or resolved, or acknowledged to resolved. A repeated identical final action is idempotent.
- `dashboard` writes standalone HTML for the latest 100 tickets and 50 runs. `metrics` writes Prometheus text exposition. Both and `show` open SQLite read-only.

Successful writes and reads return exit code zero. Runtime failures return nonzero and a JSON error object with operation, error type and message on stderr. Command-line usage errors use argparse syntax and exit code 2.
