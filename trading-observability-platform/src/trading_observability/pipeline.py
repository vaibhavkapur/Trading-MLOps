"""End-to-end instrumented paper-trade and market-data pipeline spanning all 6 trading services."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading_observability.adapters import (
    BrokerExecutionReport,
    FaultInjectionProfile,
    FixtureMarketDataAdapter,
    MarketTick,
    PaperBrokerAdapter,
    ResearchSignal,
    SyntheticResearchAdapter,
)
from trading_observability.alerting import AlertPolicyEngine, AlertRouter, MonitoringWatchdog
from trading_observability.config import PlatformConfig, load_platform_config
from trading_observability.dashboards import build_dashboard_snapshot
from trading_observability.logging_service import StructuredLogger
from trading_observability.metrics import BoundedMetricsRegistry, register_trading_metrics
from trading_observability.schema import (
    CorrelationContext,
    generate_span_id,
    generate_trace_id,
    utc_now_ns,
)
from trading_observability.tracing import TraceStore, Tracer


@dataclass
class TradePipelineResult:
    order_id: str
    client_order_id: str
    trace_id: str
    correlation_id: str
    symbol: str
    status: str
    reconciled: bool
    end_to_end_latency_ms: float
    services_traversed: List[str] = field(default_factory=list)
    error_code: Optional[str] = None


class ObservabilityPlatform:
    """Unified observability runtime composing bounded metrics, redacted logs, trace store, alerting, and watchdog."""

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        trace_db_path: Optional[Path] = None,
        log_jsonl_path: Optional[Path] = None,
    ) -> None:
        self.config = config or load_platform_config()
        self.metrics = BoundedMetricsRegistry(self.config.telemetry)
        register_trading_metrics(self.metrics, self.config.telemetry)
        self.logger = StructuredLogger(metrics=self.metrics, jsonl_path=log_jsonl_path)
        self.trace_store = TraceStore(
            db_path=trace_db_path,
            config=self.config.telemetry,
            metrics=self.metrics,
            scrubber=self.logger.scrubber,
        )
        self.tracer = Tracer(store=self.trace_store)
        self.router = AlertRouter(
            config=self.config.alerting,
            metrics=self.metrics,
            scrubber=self.logger.scrubber,
        )
        self.watchdog = MonitoringWatchdog(
            telemetry_config=self.config.telemetry,
            router=self.router,
            metrics=self.metrics,
        )
        self.alert_engine = AlertPolicyEngine(
            alert_config=self.config.alerting,
            telemetry_config=self.config.telemetry,
            metrics=self.metrics,
            trace_store=self.trace_store,
            router=self.router,
            watchdog=self.watchdog,
        )
        # Initialize risk state gauge to HEALTHY (0)
        self.metrics.set_gauge("trading_risk_state_code", 0.0, {"service": "risk"})
        # Seed initial collector heartbeats
        for svc in ("market_data", "research", "risk", "oms", "broker", "portfolio"):
            self.watchdog.record_heartbeat(svc)

    def dashboard_snapshot(self) -> Dict[str, Any]:
        return build_dashboard_snapshot(self.config, self.metrics, self.trace_store, self.router)

    def close(self) -> None:
        self.trace_store.close()


class TradingPipeline:
    """End-to-end instrumented trading pipeline: market_data -> research -> risk -> oms -> broker -> portfolio."""

    def __init__(
        self,
        platform: ObservabilityPlatform,
        market_data: Optional[FixtureMarketDataAdapter] = None,
        research: Optional[SyntheticResearchAdapter] = None,
        broker: Optional[PaperBrokerAdapter] = None,
        faults: Optional[FaultInjectionProfile] = None,
        seed: int = 20261127,
    ) -> None:
        self.platform = platform
        self.faults = faults or FaultInjectionProfile()
        self.market_data = market_data or FixtureMarketDataAdapter(seed=seed, faults=self.faults)
        self.research = research or SyntheticResearchAdapter(seed=seed)
        self.broker = broker or PaperBrokerAdapter(seed=seed, faults=self.faults)
        self._last_seq_by_venue: Dict[str, int] = {}
        self._portfolio_positions: Dict[str, float] = {}
        self._oms_idempotency_cache: Dict[str, TradePipelineResult] = {}
        self._order_counter = 0

    def process_tick(
        self,
        tick: MarketTick,
        client_order_id: Optional[str] = None,
        now_s: Optional[float] = None,
    ) -> TradePipelineResult:
        """Process one market tick through all 6 services with full trace correlation & safety gates."""
        # Gate 0: Execution policy MUST be explicitly approved for PAPER_SANDBOX
        self.platform.config.execution_policy.validate_for_paper_sandbox()

        self._order_counter += 1
        order_id = f"ORD-{self._order_counter:06d}"
        cid = client_order_id or f"CLORD-{tick.feed_batch_id}-{self._order_counter:06d}"
        if cid in self._oms_idempotency_cache:
            return self._oms_idempotency_cache[cid]

        trace_id = generate_trace_id(f"{tick.feed_batch_id}:{cid}")
        correlation_id = f"corr-{tick.feed_batch_id}-{order_id}"
        services_traversed: List[str] = []
        total_sim_ms = 0.0

        # 1. MARKET DATA SERVICE
        md_ctx = CorrelationContext(
            trace_id=trace_id,
            span_id=generate_span_id(f"{trace_id}:market_data"),
            correlation_id=correlation_id,
            service="market_data",
            environment=self.platform.config.environment,
            feed_batch_id=tick.feed_batch_id,
            order_id=order_id,
        )
        self.platform.watchdog.record_heartbeat("market_data", now_s=now_s)
        prev_seq = self._last_seq_by_venue.get(tick.venue)
        seq_gap = prev_seq is not None and tick.seq > prev_seq + 1
        self._last_seq_by_venue[tick.venue] = tick.seq
        feed_age = tick.event_age_seconds
        md_status = "stale" if (feed_age > self.platform.config.alerting.stale_market_data_seconds or seq_gap) else "ok"
        md_ms = 1.2
        total_sim_ms += md_ms

        with self.platform.tracer.start_span(
            md_ctx,
            operation="market_data.ingest_tick",
            attributes={
                "venue": tick.venue,
                "symbol": tick.symbol,
                "seq": tick.seq,
                "feed_age_seconds": round(feed_age, 4),
                "sequence_gap": seq_gap,
            },
            simulated_duration_ms=md_ms,
        ) as md_span:
            services_traversed.append("market_data")
            self.platform.metrics.set_gauge("trading_market_data_age_seconds", feed_age, {"venue": tick.venue})
            self.platform.metrics.inc_counter(
                "trading_market_data_ticks_total",
                1.0,
                {"venue": tick.venue, "symbol": tick.symbol, "status": md_status},
            )
            if seq_gap:
                self.platform.metrics.inc_counter("trading_market_data_sequence_gaps_total", 1.0, {"venue": tick.venue})
                md_span.status = "degraded"
            self.platform.logger.info(
                "market_data",
                f"Tick ingested for {tick.symbol} on {tick.venue} seq={tick.seq}",
                ctx=md_ctx,
                attributes={"venue": tick.venue, "symbol": tick.symbol, "feed_age_seconds": round(feed_age, 4)},
            )

        # Fail-safe halt if feed is stale or has a sequence gap
        if md_status == "stale":
            self.platform.metrics.inc_counter(
                "trading_orders_total",
                1.0,
                {"service": "oms", "symbol": tick.symbol, "status": "rejected"},
            )
            res = TradePipelineResult(
                order_id=order_id,
                client_order_id=cid,
                trace_id=trace_id,
                correlation_id=correlation_id,
                symbol=tick.symbol,
                status="halted_stale_feed",
                reconciled=True,
                end_to_end_latency_ms=round(total_sim_ms, 3),
                services_traversed=services_traversed,
                error_code="STALE_MARKET_DATA",
            )
            self._oms_idempotency_cache[cid] = res
            return res

        # 2. RESEARCH SERVICE (propagating W3C traceparent headers)
        headers = md_ctx.to_headers()
        res_ctx = CorrelationContext.from_headers(
            headers,
            service="research",
            new_span_id=generate_span_id(f"{trace_id}:research"),
            environment=self.platform.config.environment,
        )
        self.platform.watchdog.record_heartbeat("research", now_s=now_s)
        signal: ResearchSignal = self.research.generate_signal(tick)
        res_ms = 2.4
        total_sim_ms += res_ms

        res_attrs: Dict[str, Any] = {
            "signal_id": signal.signal_id,
            "strategy_id": signal.strategy_id,
            "symbol": signal.symbol,
            "side": signal.side,
            "confidence": signal.confidence,
        }
        if self.faults.inject_sensitive_log_attempt:
            # Attempt to attach confidential strategy params + raw API key to verify scrubber blocks it
            res_attrs.update(signal.confidential_params)
            res_attrs["api_key"] = "sk_live_SIMULATED999988887777"

        with self.platform.tracer.start_span(
            res_ctx,
            operation="research.generate_signal",
            attributes=res_attrs,
            simulated_duration_ms=res_ms,
        ):
            services_traversed.append("research")
            self.platform.logger.info(
                "research",
                f"Generated signal {signal.signal_id} for {signal.symbol}",
                ctx=res_ctx,
                attributes=res_attrs,
            )

        # 3. RISK SERVICE
        risk_ctx = res_ctx.child(
            service="risk",
            span_id=generate_span_id(f"{trace_id}:risk"),
            order_id=order_id,
            client_order_id=cid,
            strategy_id=signal.strategy_id,
        )
        self.platform.watchdog.record_heartbeat("risk", now_s=now_s)
        risk_ms = 1.8
        total_sim_ms += risk_ms
        risk_passed = not self.faults.force_risk_breach

        with self.platform.tracer.start_span(
            risk_ctx,
            operation="risk.pre_trade_check",
            attributes={"symbol": signal.symbol, "check_passed": risk_passed},
            simulated_duration_ms=risk_ms,
        ) as risk_span:
            services_traversed.append("risk")
            if not risk_passed:
                risk_span.status = "breached"
                self.platform.metrics.set_gauge("trading_risk_state_code", 2.0, {"service": "risk"})
                self.platform.metrics.inc_counter(
                    "trading_orders_total",
                    1.0,
                    {"service": "oms", "symbol": signal.symbol, "status": "rejected"},
                )
                self.platform.logger.warning(
                    "risk",
                    f"Pre-trade risk breach halted order {order_id}",
                    ctx=risk_ctx,
                    attributes={"symbol": signal.symbol, "decision": "REJECT_KILL_SWITCH"},
                )
                res = TradePipelineResult(
                    order_id=order_id,
                    client_order_id=cid,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                    symbol=signal.symbol,
                    status="rejected_risk",
                    reconciled=True,
                    end_to_end_latency_ms=round(total_sim_ms, 3),
                    services_traversed=services_traversed,
                    error_code="RISK_STATE_BREACH",
                )
                self._oms_idempotency_cache[cid] = res
                return res
            self.platform.metrics.set_gauge("trading_risk_state_code", 0.0, {"service": "risk"})
            self.platform.logger.info(
                "risk",
                f"Pre-trade risk check passed for {order_id}",
                ctx=risk_ctx,
                attributes={"symbol": signal.symbol, "decision": "APPROVE"},
            )

        # 4. OMS SERVICE
        oms_ctx = risk_ctx.child(
            service="oms",
            span_id=generate_span_id(f"{trace_id}:oms"),
            order_id=order_id,
            client_order_id=cid,
        )
        self.platform.watchdog.record_heartbeat("oms", now_s=now_s)
        queue_lag_ms = 3.5 + self.faults.oms_extra_queue_lag_ms
        total_sim_ms += queue_lag_ms
        oms_log_attrs: Dict[str, Any] = {
            "symbol": signal.symbol,
            "side": signal.side,
            "queue_lag_ms": round(queue_lag_ms, 3),
        }
        oms_msg = f"OMS routing paper order {order_id} for {signal.symbol}"
        if self.faults.inject_sensitive_log_attempt:
            oms_log_attrs["limit_price"] = signal.reference_price
            oms_log_attrs["order_qty"] = signal.suggested_qty
            oms_log_attrs["client_account"] = "ACCT-99887766"
            oms_log_attrs["authorization"] = "Bearer SECRET_TOKEN_DO_NOT_LEAK_12345"
            oms_msg += " limit_price=100.05 client_account=ACCT-99887766 Bearer SECRET_TOKEN_DO_NOT_LEAK_12345"

        with self.platform.tracer.start_span(
            oms_ctx,
            operation="oms.dispatch_order",
            attributes=oms_log_attrs,
            simulated_duration_ms=queue_lag_ms,
        ):
            services_traversed.append("oms")
            self.platform.metrics.observe_histogram(
                "trading_oms_queue_lag_seconds", queue_lag_ms / 1000.0, {"service": "oms"}
            )
            self.platform.logger.info("oms", oms_msg, ctx=oms_ctx, attributes=oms_log_attrs)

        # 5. BROKER SERVICE
        broker_ctx = oms_ctx.child(
            service="broker",
            span_id=generate_span_id(f"{trace_id}:broker"),
            order_id=order_id,
            client_order_id=cid,
        )
        self.platform.watchdog.record_heartbeat("broker", now_s=now_s)
        report: BrokerExecutionReport = self.broker.submit_paper_order(
            order_id=order_id,
            client_order_id=cid,
            symbol=signal.symbol,
            side=signal.side,
            qty=signal.suggested_qty,
            limit_price=signal.reference_price,
            currency=signal.currency,
        )
        total_sim_ms += report.ack_latency_ms
        with self.platform.tracer.start_span(
            broker_ctx,
            operation="broker.execute_paper_order",
            attributes={
                "venue": report.venue,
                "symbol": report.symbol,
                "broker_exec_id": report.broker_exec_id,
                "status": report.status,
                "ack_latency_ms": round(report.ack_latency_ms, 3),
            },
            simulated_duration_ms=report.ack_latency_ms,
            simulated_ingest_delay_s=self.faults.delayed_span_ingest_seconds,
        ) as broker_span:
            services_traversed.append("broker")
            if report.status != "filled":
                broker_span.status = "rejected"
            self.platform.metrics.observe_histogram(
                "trading_broker_ack_latency_seconds",
                report.ack_latency_ms / 1000.0,
                {"venue": report.venue, "status": report.status},
            )
            self.platform.metrics.inc_counter(
                "trading_orders_total",
                1.0,
                {"service": "oms", "symbol": report.symbol, "status": report.status},
            )
            self.platform.logger.info(
                "broker",
                f"Paper broker report {report.broker_exec_id} status={report.status}",
                ctx=broker_ctx,
                attributes={"venue": report.venue, "symbol": report.symbol, "status": report.status},
            )

        # 6. PORTFOLIO & RECONCILIATION SERVICE
        port_ctx = broker_ctx.child(
            service="portfolio",
            span_id=generate_span_id(f"{trace_id}:portfolio"),
            order_id=order_id,
            client_order_id=cid,
        )
        self.platform.watchdog.record_heartbeat("portfolio", now_s=now_s)
        port_ms = 1.5
        total_sim_ms += port_ms
        reconciled = True
        with self.platform.tracer.start_span(
            port_ctx,
            operation="portfolio.apply_fill_and_reconcile",
            attributes={"symbol": report.symbol, "status": report.status},
            simulated_duration_ms=port_ms,
            simulated_ingest_delay_s=self.faults.delayed_span_ingest_seconds,
        ) as port_span:
            services_traversed.append("portfolio")
            if report.status == "filled":
                signed = report.filled_qty if report.side == "BUY" else -report.filled_qty
                self._portfolio_positions[report.symbol] = (
                    self._portfolio_positions.get(report.symbol, 0.0) + signed
                )
                broker_pos = self.broker.fetch_broker_position_qty(report.symbol)
                port_pos = self._portfolio_positions[report.symbol]
                if abs(broker_pos - port_pos) > 1e-6:
                    reconciled = False
                    port_span.status = "mismatch"
                    self.platform.metrics.inc_counter(
                        "trading_reconciliation_mismatches_total",
                        1.0,
                        {"venue": report.venue, "symbol": report.symbol},
                    )
                    self.platform.logger.error(
                        "portfolio",
                        f"Reconciliation mismatch for {report.symbol} after {order_id}",
                        ctx=port_ctx,
                        attributes={"symbol": report.symbol, "reconciled": False},
                    )
                else:
                    self.platform.logger.info(
                        "portfolio",
                        f"Portfolio updated and reconciled for {order_id}",
                        ctx=port_ctx,
                        attributes={"symbol": report.symbol, "reconciled": True},
                    )

        terminal_status = "filled" if report.status == "filled" else "rejected"
        self.platform.metrics.observe_histogram(
            "trading_pipeline_end_to_end_latency_seconds",
            total_sim_ms / 1000.0,
            {"status": terminal_status},
        )
        result = TradePipelineResult(
            order_id=order_id,
            client_order_id=cid,
            trace_id=trace_id,
            correlation_id=correlation_id,
            symbol=report.symbol,
            status=terminal_status,
            reconciled=reconciled,
            end_to_end_latency_ms=round(total_sim_ms, 3),
            services_traversed=services_traversed,
            error_code=None if reconciled and terminal_status == "filled" else (
                "RECONCILIATION_DRIFT" if not reconciled else "ORDER_REJECTED"
            ),
        )
        self._oms_idempotency_cache[cid] = result
        return result

    def run_batch(self, limit: int = 10, now_s: Optional[float] = None) -> List[TradePipelineResult]:
        """Pull a batch of ticks from MarketDataAdapter; handles lost feed explicitly."""
        self.platform.config.execution_policy.validate_for_paper_sandbox()
        ticks = self.market_data.next_ticks(limit=limit)
        if not ticks:
            # Record feed absence so stale_market_data alert fires
            self.platform.metrics.set_gauge(
                "trading_market_data_age_seconds",
                self.platform.config.alerting.stale_market_data_seconds + 5.0,
                {"venue": "SYNTH_EXCH_A"},
            )
            self.platform.metrics.inc_counter(
                "trading_market_data_ticks_total",
                1.0,
                {"venue": "SYNTH_EXCH_A", "symbol": "SYN-EQ-01", "status": "stale"},
            )
            self.platform.logger.error(
                "market_data",
                "Market data feed lost: zero ticks received from adapter",
                attributes={"venue": "SYNTH_EXCH_A", "condition": "lost_feed"},
            )
            return []
        return [self.process_tick(t, now_s=now_s) for t in ticks]
