# Operator runbook

## Bring up a monitor

Run `scripts/setup` and `scripts/validate`. Confirm the approved model version, units, horizon, baseline interval, seasonality assumptions and per-feature permissions. Have the local registry owner review and register the configuration. The default reviewer is the effective OS account at registration. Use `--reviewer-account` to designate a different existing account.

Keep SQLite and any exports in private directories. New CLI files use an owner-only umask. Use a trusted writer account for imports and scheduled work, the registered reviewer for ticket transitions, and read-only exported artifacts for viewers. If the reviewer is a separate OS user, provision narrowly scoped filesystem ACLs for the database and SQLite's journal directory. Shell redirection inherits the caller's umask, so use `umask 077`. Never embed credentials in configuration or notes.

## Schedule and observe

Schedule `bin/drift tick --registry ID` with cron or a local scheduler after imports. Use absolute paths and set `DRIFT_DB`. Choose a cadence no longer than the configured step. The supplied fixture is historical. Use the explicit cutoffs in the demo for that registry. A first wall-clock tick catches up all elapsed windows through the current time.

For example, the following commands run one due check and publish operator artifacts after success. Set the working directory to this repository and configure the registry ID for your feed. Use a single publisher. A temporary file and rename keep readers from seeing a partial export.

```sh
set -eu
umask 077
export DRIFT_DB="$PWD/var/example.sqlite3"
bin/drift tick --registry direction-hourly-v1
bin/drift metrics > var/drift.prom.tmp
mv var/drift.prom.tmp var/drift.prom
bin/drift dashboard > var/dashboard.html.tmp
mv var/dashboard.html.tmp var/dashboard.html
```

Configure the Prometheus node exporter's textfile collector to read the authorized metrics export, or inspect `bin/drift metrics` directly. Watch the last evaluated cutoff relative to the expected schedule, failed operations, open tickets and cooldown dispositions. Scheduler exit status and stderr cover failures that occur before SQLite is writable. A healthy idle retry may return an empty run list.

## Investigate and recover

- **Missingness or label coverage.** Expand the evidence and check reference/current counts, the affected field, null or overdue fraction, and availability timestamps. Fix the feed and ingest newly available records using their true availability time. Run another tick. Use a new registry for corrected immutable history.
- **Inconclusive or pending.** Check expected volume, the UTC hour, horizon and grace. Wait for enough eligible rows or labels. Investigate missing source delivery when complete windows remain thin.
- **Distribution shift.** Inspect matched-hour sample counts and KS distances. Verify units and model version before interpreting a change. A distribution flag alone supplies no return estimate.
- **Performance degradation.** Review mature sample size, coverage, accuracy, Brier and calibration together. Inspect label definitions and upstream quality before escalating to the model owner.
- **Ticket review.** Use `bin/drift show RUN_ID` for the original scores. Acknowledge or resolve using `bin/drift review TICKET_ID acknowledged --note "reason"`. Re-export the dashboard to see audit history. For a cooldown decision, follow its referenced ticket. Closing it requires an explicit human action.
- **Crash, conflict or lock timeout.** Keep the last successful artifacts. Retry the same atomic import or cutoff after the writer releases its lock. Validate the reported row or state for a contract conflict. Use SQLite's online backup facility or stop all writers before taking a file copy. Restore to a private path, verify database integrity and the latest watermark, then resume. Back up the configured reviewer identity along with the data.

## Security, provenance and approval boundary

The local review covered input schema validation, parameterized SQLite values, escaped dashboard content, read-only exports, role checks, retry behavior and absence of network or trading adapters. SQLite write access confers trust over the whole file. Host administrators can alter it. A multi-user service would need independently enforced authorization and external audit storage.

The fixtures and implementation were created for this project from the supplied plan. The manifest records the generation process. The prototype includes no historical market data, copied personal code, provider credentials or external datasets. Organizational IP ownership, entitlements for real data, asset class, strategy horizon and deployment sign-off remain decisions for the relevant owners. Paper/sandbox connectivity and any real-capital use require separate approval. No such approval is asserted here.
