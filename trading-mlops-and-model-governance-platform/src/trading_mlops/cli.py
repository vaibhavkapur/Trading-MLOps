"""CLI entrypoint for inspecting state, lineage, audit chain, and governance posture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from trading_mlops.config import PlatformConfig
from trading_mlops.platform import MlopsPlatform


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-mlops",
        description="Trading MLOps and Model Governance Platform CLI",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".tmp_run/platform_state"),
        help="Path to platform state directory.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/platform_config.json"),
        help="Path to versioned platform configuration JSON.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-audit", help="Verify cryptographic hash chain of audit_log.jsonl.")
    sub.add_parser("governance-review", help="Print full governance review JSON summary.")

    lin = sub.add_parser("inspect-lineage", help="Inspect complete lineage DAG for an artifact.")
    lin.add_argument("artifact_id", help="Registered artifact_id.")

    exp = sub.add_parser(
        "export-inference-manifest",
        help="Verify artifact and export Project #16 compatible inference manifest.",
    )
    exp.add_argument("artifact_id", help="Registered artifact_id.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = PlatformConfig.load(args.config) if args.config.exists() else PlatformConfig()
    platform = MlopsPlatform(state_dir=args.state_dir, config=cfg)

    if args.command == "verify-audit":
        result = platform.audit_log.verify_chain()
    elif args.command == "governance-review":
        result = platform.governance_review()
    elif args.command == "inspect-lineage":
        result = platform.inspect_lineage(args.artifact_id)
    elif args.command == "export-inference-manifest":
        result = platform.export_inference_manifest(args.artifact_id)
    else:
        parser.error(f"Unknown command: {args.command}")
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
