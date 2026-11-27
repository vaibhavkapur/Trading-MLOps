# MVP verification

The reference evaluation uses the checked-in version-1 policy and deterministic generator. Raw evidence includes every tuning and holdout window, timestamp, seed, distance, performance deterioration, status, ticket disposition and run ID. Run `scripts/ci` to compare a clean replay with the retained result. [Raw windows and confidence intervals](evaluation.json) · [Test output](tests.txt)

## Chronological evaluation

The reference contains 13,440 observations across January 1–7, 2026 UTC. Each hourly window has 80 forecasts except the explicit small-sample cases (12). Each hour is compared to its own historical UTC-hour stratum. Label visibility freezes at each cutoff. The evaluation scores each window one hour after it closes. The separate delayed-label scenario exercises earlier, overdue and recovered states.

The next 80 windows form the tuning set. The selection rule requires at most one flag in 48 stable windows and detection in at least 15 of 16 windows for each strong feature and outcome injection. Three initial candidates flagged 13, 4 and 2 stable windows. All detected both sets of 16 strong injections. The last candidate's two false flags came from accuracy and calibration. Raising those thresholds to 0.18 yielded zero stable flags while retaining all strong detections. The final policy keeps KS at 0.30 and Brier deterioration at 0.08. Missingness, coverage and cooldown are fixed operational settings tested by scenarios.

The final holdout uses a separate seed sequence and the next 252 hours. Detection is measured on raw threshold crossings before cooldown. Approximate 95% Wilson intervals describe finite simulation counts conditional on this generator and fixed reference. They do not estimate real financial performance.

- **Stable false alerts.** 0 of 96 stable windows triggered any quality, distribution or performance signal. The approximate 95% interval is 0–3.85%.
- **Abrupt feature shift.** A 1.5-standard-deviation momentum shift was detected in 24 of 24 windows (86.2–100%).
- **Outcome degradation.** Flipping the binary outcome while preserving the forecast probabilities triggered performance degradation in 24 of 24 windows (86.2–100%).
- **Prediction distribution.** Adding 0.35 to forecast probabilities, capped at one, triggered prediction KS in 12 of 12 windows.
- **Gradual shift.** Across 12 trajectories, detections were 0, 0, 2, 8, 12 and 12 at shifts of 0, 0.25, 0.5, 0.75, 1.0 and 1.5 standard deviations respectively. Small shifts were frequently missed.
- **Input quality and small samples.** All 12 missingness windows raised the expected spread quality signal. All 12 underfilled windows were inconclusive.

## Milestone evidence

- **M1 · Foundation.** The setup check, immutable registry, schema tests and synthetic generator exercise the complete adapter contract. Replayed input and concurrent evaluation tests cover idempotency and process restart.
- **M2 · Distribution tests.** Unit tests verify ties, empirical CDF distances and sample handling. Scenario tests verify seasonality, missingness, empty windows and both abrupt and gradual shifts.
- **M3 · Delayed labels.** Integration tests verify frozen baselines, cutoff filtering and late observations. Scenario tests follow pending labels through a coverage incident and later recovery while retaining the first run. Calibration, accuracy and Brier have analytical unit-test expectations.
- **M4 · Review and monitoring.** A five-window alert storm produces two tickets at the cooldown boundary. The other three decisions link to those tickets. Tests cover unauthorized reviews, review history, safe rendering, read-only database access and failure metrics. The runnable demo exports a dashboard and metrics after an acknowledged incident.

## Delivery boundary

The source, operator runbook, schema, architecture diagram, reference configuration and evaluation are versioned together. The CI entry point executes setup, unit/integration/scenario tests, demo and exact evidence replay (allowing runtime-version metadata to differ). All artifacts use locally generated synthetic data. The reviewer workflow ends at ticket resolution. Local prototype checks and operational limits are documented in the [runbook](../docs/runbook.md). Organizational approval for real data, ownership and deployment remains outstanding.
