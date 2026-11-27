"""CLI entry point for demo, fault scenarios, alert drills, and local observability HTTP server."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from trading_observability.adapters import FixtureMarketDataAdapter
from trading_observability.config import load_platform_config
from trading_observability.pipeline import ObservabilityPlatform, TradingPipeline
from trading_observability.scenarios import DEFAULT_FIXTURE, run_all_scenarios
from trading_observability.schema import ViewerRole
from trading_observability.server import create_observability_server


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = load_platform_config().with_paper_sandbox_enabled()
    reports_dir = Path(args.reports_dir)
    trace_db = reports_dir / "demo_traces.sqlite3"
    log_jsonl = reports_dir / "demo_logs.jsonl"
    if trace_db.exists():
        trace_db.unlink()
    if log_jsonl.exists():
        log_jsonl.unlink()

    plat = ObservabilityPlatform(config=cfg, trace_db_path=trace_db, log_jsonl_path=log_jsonl)
    try:
        pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=args.seed),
            seed=args.seed,
        )
        results = pipe.run_batch(limit=args.orders)
        plat.alert_engine.evaluate()
        snapshot = plat.dashboard_snapshot()
        sample_trace = (
            plat.trace_store.get_trace_by_order_id(results[0].order_id, role=ViewerRole.VIEWER)
            if results
            else {}
        )
        otlp_export = plat.trace_store.export_otlp_json(
            trace_id=results[0].trace_id if results else None
        )

        _write_json(reports_dir / "demo_dashboard_snapshot.json", snapshot)
        _write_json(reports_dir / "demo_sample_trace_otlp.json", otlp_export)
        (reports_dir / "demo_metrics.prom").write_text(
            plat.metrics.export_prometheus(), encoding="utf-8"
        )

        summary = {
            "orders_processed": len(results),
            "sample_order_id": results[0].order_id if results else None,
            "sample_trace_id": results[0].trace_id if results else None,
            "sample_trace_services": sample_trace.get("services_covered", []),
            "total_spans": plat.trace_store.total_span_count(),
            "cardinality_series": snapshot["observability_self_health"]["cardinality"]["total_series"],
            "active_alerts": len(snapshot["active_alerts"]),
            "artifacts_written": [
                str(reports_dir / "demo_dashboard_snapshot.json"),
                str(reports_dir / "demo_sample_trace_otlp.json"),
                str(reports_dir / "demo_metrics.prom"),
                str(log_jsonl),
            ],
        }
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        plat.close()


def cmd_scenarios(args: argparse.Namespace) -> int:
    report = run_all_scenarios(seed=args.seed, load_orders=args.load_orders)
    reports_dir = Path(args.reports_dir)
    json_path = reports_dir / "scenario_validation_report.json"
    md_path = reports_dir / "scenario_validation_report.md"
    _write_json(json_path, report)
    md_path.write_text(_render_scenario_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "all_scenarios_passed": report["all_scenarios_passed"],
                "passed_count": report["passed_count"],
                "total_scenarios": report["total_scenarios"],
                "json_report": str(json_path),
                "markdown_report": str(md_path),
            },
            indent=2,
        )
    )
    return 0 if report["all_scenarios_passed"] else 1


def _render_scenario_markdown(report: dict) -> str:
    lines = [
        "# Trading Observability Platform — Fault Scenarios & Load Baseline Evidence",
        "",
        f"- **Generated UTC:** `{report['generated_at_utc']}`",
        f"- **Fixed Seed:** `{report['seed']}`",
        f"- **Result:** `{report['passed_count']}/{report['total_scenarios']}` scenarios passed (`all_scenarios_passed={report['all_scenarios_passed']}`)",
        "",
        "## Fault Scenario Matrix",
        "",
        "| Scenario | Passed | Alerts Fired | Summary |",
        "|---|---|---|---|",
    ]
    for sc in report["scenarios"]:
        alerts = ", ".join(f"`{a}`" for a in sc["alerts_fired"]) if sc["alerts_fired"] else "none"
        status = "PASS" if sc["passed"] else "FAIL"
        lines.append(f"| `{sc['name']}` | **{status}** | {alerts} | {sc['summary']} |")

    bm = report["load_benchmark"]
    res = bm["results"]
    env = bm["environment"]
    lines.extend(
        [
            "",
            "## Local Synthetic Load Baseline (Measured — Not a Production SLA)",
            "",
            f"> {bm['disclaimer']}",
            "",
            f"- **Measurement Status:** `{bm['measurement_status']}`",
            f"- **Environment:** `{env['os']}` | Python `{env['python_version']}` | Arch `{env['machine']}`",
            f"- **Sample Size:** `{res['orders_processed']}` synthetic paper orders (`{res['spans_ingested']}` spans, `{res['logs_emitted']}` redacted logs)",
            f"- **Wall Clock:** `{res['wall_clock_seconds']}s` (`{res['local_throughput_orders_per_sec']}` orders/sec, `{res['wall_clock_ms_per_order']} ms/order`)",
            f"- **Simulated End-to-End Pipeline Latency (ms):** p50=`{res['simulated_pipeline_latency_ms']['p50']}`, p95=`{res['simulated_pipeline_latency_ms']['p95']}`, p99=`{res['simulated_pipeline_latency_ms']['p99']}`",
            f"- **Metrics Cardinality:** `{res['cardinality_total_series']}/{res['cardinality_max_series']}` active series (utilization `{res['cardinality_utilization_ratio']:.2%}`, overflows `{res['cardinality_overflow_events']}`)",
            "",
        ]
    )
    return "\n".join(lines)


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_platform_config().with_paper_sandbox_enabled()
    plat = ObservabilityPlatform(config=cfg)
    pipe = TradingPipeline(
        plat,
        market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=args.seed),
        seed=args.seed,
    )
    pipe.run_batch(limit=args.seed_orders)
    plat.alert_engine.evaluate()
    server = create_observability_server(plat, host=args.host, port=args.port)
    print(f"Observability server listening on http://{args.host}:{args.port} (/metrics, /dashboard, /alerts, /traces/<order_id>)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        plat.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-obs", description="Trading Observability Platform CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="Run end-to-end paper trade & market data observability demo")
    p_demo.add_argument("--orders", type=int, default=10)
    p_demo.add_argument("--seed", type=int, default=20261127)
    p_demo.add_argument("--reports-dir", type=str, default="reports")
    p_demo.set_defaults(func=cmd_demo)

    p_scen = sub.add_parser("scenarios", help="Run all 7+ fault scenarios and local synthetic load benchmark")
    p_scen.add_argument("--seed", type=int, default=20261127)
    p_scen.add_argument("--load-orders", type=int, default=200)
    p_scen.add_argument("--reports-dir", type=str, default="reports")
    p_scen.set_defaults(func=cmd_scenarios)

    p_serve = sub.add_parser("serve", help="Run local HTTP metrics/dashboard/trace endpoint")
    p_serve.add_argument("--host", type=str, default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=9108)
    p_serve.add_argument("--seed", type=int, default=20261127)
    p_serve.add_argument("--seed-orders", type=int, default=5)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
