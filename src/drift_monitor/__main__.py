import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .contracts import Invalid, canonical, stamp
from .engine import evaluate, review, tick
from .storage import connect, ingest, register
from .views import dashboard, evidence, metrics


def records(stream):
    for number, line in enumerate(stream, 1):
        if line.strip():
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise Invalid(f"invalid JSON at line {number}") from exc


def parser():
    cli = argparse.ArgumentParser(description="Local financial model drift monitor")
    cli.add_argument("--db", default=os.environ.get("DRIFT_DB", "var/monitor.sqlite3"))
    commands = cli.add_subparsers(dest="command", required=True)
    reg = commands.add_parser("register")
    reg.add_argument("config")
    reg.add_argument("--reviewer-account", help="local OS account allowed to review this registry")
    commands.add_parser("ingest").add_argument("file", help="NDJSON, or - for stdin")
    ev = commands.add_parser("evaluate")
    ev.add_argument("--end", required=True)
    ev.add_argument("--as-of", required=True)
    worker = commands.add_parser("tick")
    worker.add_argument("--as-of", help="UTC evaluation cutoff (default: current time)")
    for op in ("ingest", "evaluate", "tick"):
        commands.choices[op].add_argument("--registry", required=True)
    commands.add_parser("show").add_argument("id", type=int)
    for op in ("dashboard", "metrics"):
        commands.add_parser(op)
    rev = commands.add_parser("review")
    rev.add_argument("id", type=int)
    rev.add_argument("state", choices=("acknowledged", "resolved"))
    rev.add_argument("--note", required=True)
    return cli


def main():
    args = parser().parse_args()
    op = args.command
    readonly = op in ("dashboard", "metrics", "show")
    os.umask(0o077)
    try:
        if not readonly:
            Path(args.db).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(connect(args.db, readonly)) as db:
            try:
                if op == "register":
                    result = {"config_sha256": register(db, json.loads(Path(args.config).read_text()), args.reviewer_account)}
                elif op == "ingest":
                    if args.file == "-":
                        result = ingest(db, args.registry, records(sys.stdin))
                    else:
                        with open(args.file, encoding="utf-8") as stream:
                            result = ingest(db, args.registry, records(stream))
                elif op == "evaluate":
                    result = evidence(db, evaluate(db, args.registry, args.end, args.as_of))
                elif op == "tick":
                    result = {"run_ids": tick(db, args.registry, args.as_of or stamp(datetime.now(timezone.utc)))}
                elif op == "review":
                    review(db, args.id, args.state, args.note)
                    result = {"ticket": args.id, "state": args.state}
                elif op == "show":
                    result = evidence(db, args.id)
                else:
                    print(dashboard(db) if op == "dashboard" else metrics(db), end="")
                    return 0
                print(canonical(result))
            except (ValueError, OSError, sqlite3.Error, OverflowError):
                if not readonly:
                    db.execute("""INSERT INTO failures VALUES(?,1) ON CONFLICT(operation)
                               DO UPDATE SET count=count+1""", (op,))
                raise
    except (ValueError, OSError, sqlite3.Error, OverflowError) as exc:
        print(canonical({"error": type(exc).__name__, "operation": op, "message": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
