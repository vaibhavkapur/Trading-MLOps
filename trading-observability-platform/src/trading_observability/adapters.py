"""Clean adapter contracts for foundation boundaries (market data, research, latency probe, broker reconciliation)
and deterministic synthetic paper/sandbox implementations with fault injection.

No external personal repository code is copied; external integration uses these versioned contracts.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from trading_observability.schema import ns_to_utc_iso, parse_utc_iso_to_ns, utc_now_ns


@dataclass(frozen=True)
class MarketTick:
    feed_batch_id: str
    seq: int
    symbol: str
    venue: str
    bid: float
    ask: float
    currency: str
    event_time_utc: str
    available_time_utc: str

    @property
    def mid_price(self) -> float:
        return round((self.bid + self.ask) / 2.0, 6)

    @property
    def event_age_seconds(self) -> float:
        ev = parse_utc_iso_to_ns(self.event_time_utc)
        av = parse_utc_iso_to_ns(self.available_time_utc)
        return max(0.0, (av - ev) / 1_000_000_000.0)


@dataclass(frozen=True)
class ResearchSignal:
    signal_id: str
    strategy_id: str
    symbol: str
    side: str  # BUY | SELL
    confidence: float
    suggested_qty: float
    reference_price: float
    currency: str
    feed_batch_id: str
    # Confidential parameters intentionally kept separate so scrubber tests can verify redaction
    confidential_params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerExecutionReport:
    order_id: str
    client_order_id: str
    broker_exec_id: str
    venue: str
    symbol: str
    side: str
    status: str  # filled | rejected | timeout
    filled_qty: float
    avg_fill_price: float
    currency: str
    ack_latency_ms: float
    reject_reason: Optional[str] = None
    reconciliation_qty_override: Optional[float] = None  # optional fault injection for drift


@dataclass
class FaultInjectionProfile:
    """Deterministic fault injection toggles for scenario & resilience testing."""

    lost_feed: bool = False
    stale_feed_age_seconds: float = 0.0
    inject_sequence_gap: bool = False
    slow_broker_latency_ms: float = 0.0
    oms_extra_queue_lag_ms: float = 0.0
    force_broker_reject: bool = False
    force_reconciliation_mismatch: bool = False
    force_risk_breach: bool = False
    delayed_span_ingest_seconds: float = 0.0
    inject_sensitive_log_attempt: bool = False


class MarketDataAdapter(Protocol):
    def next_ticks(self, limit: int = 10) -> List[MarketTick]:
        ...


class ResearchSignalAdapter(Protocol):
    def generate_signal(self, tick: MarketTick) -> ResearchSignal:
        ...


class BrokerReconciliationAdapter(Protocol):
    def submit_paper_order(
        self,
        order_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        currency: str,
    ) -> BrokerExecutionReport:
        ...

    def fetch_broker_position_qty(self, symbol: str) -> float:
        ...


class FixtureMarketDataAdapter:
    """Loads permitted synthetic market ticks from JSONL fixtures or generates deterministic synthetic ticks."""

    def __init__(
        self,
        fixture_path: Optional[Path] = None,
        seed: int = 20261127,
        faults: Optional[FaultInjectionProfile] = None,
    ) -> None:
        self._fixture_path = fixture_path
        self._rng = random.Random(seed)
        self._faults = faults or FaultInjectionProfile()
        self._seq_by_venue: Dict[str, int] = {}
        self._batch_counter = 0

    def next_ticks(self, limit: int = 10) -> List[MarketTick]:
        if self._faults.lost_feed:
            return []

        ticks: List[MarketTick] = []
        if self._fixture_path and Path(self._fixture_path).exists():
            for line in Path(self._fixture_path).read_text(encoding="utf-8").splitlines():
                if not line.strip() or len(ticks) >= limit:
                    continue
                raw = json.loads(line)
                seq = int(raw["seq"])
                if self._faults.inject_sequence_gap and len(ticks) == 2:
                    seq += 5
                av_time = raw["available_time_utc"]
                if self._faults.stale_feed_age_seconds > 0:
                    ev_ns = parse_utc_iso_to_ns(raw["event_time_utc"])
                    av_time = ns_to_utc_iso(ev_ns + int(self._faults.stale_feed_age_seconds * 1_000_000_000))
                ticks.append(
                    MarketTick(
                        feed_batch_id=str(raw["feed_batch_id"]),
                        seq=seq,
                        symbol=str(raw["symbol"]),
                        venue=str(raw["venue"]),
                        bid=float(raw["bid"]),
                        ask=float(raw["ask"]),
                        currency=str(raw.get("currency", "USD")),
                        event_time_utc=str(raw["event_time_utc"]),
                        available_time_utc=av_time,
                    )
                )
            return ticks

        symbols = [
            ("SYN-EQ-01", "SYNTH_EXCH_A", 100.0),
            ("SYN-EQ-02", "SYNTH_EXCH_A", 245.5),
            ("SYN-EQ-03", "SYNTH_EXCH_B", 52.25),
            ("SYN-FX-01", "SYNTH_EXCH_B", 1.085),
            ("SYN-FUT-01", "SYNTH_EXCH_A", 4520.0),
        ]
        base_ns = utc_now_ns()
        for i in range(limit):
            self._batch_counter += 1
            sym, venue, base_px = symbols[i % len(symbols)]
            seq = self._seq_by_venue.get(venue, 0) + 1
            if self._faults.inject_sequence_gap and i == 2:
                seq += 4
            self._seq_by_venue[venue] = seq
            drift = self._rng.uniform(-0.005, 0.005)
            mid = round(base_px * (1.0 + drift), 4)
            spread = round(max(0.0004, mid * 0.001), 4)
            ev_ns = base_ns + i * 50_000_000
            extra_age = int(self._faults.stale_feed_age_seconds * 1_000_000_000)
            av_ns = ev_ns + 10_000_000 + extra_age
            ticks.append(
                MarketTick(
                    feed_batch_id=f"FB-SYN-{self._batch_counter:05d}",
                    seq=seq,
                    symbol=sym,
                    venue=venue,
                    bid=round(mid - spread / 2.0, 4),
                    ask=round(mid + spread / 2.0, 4),
                    currency="USD",
                    event_time_utc=ns_to_utc_iso(ev_ns),
                    available_time_utc=ns_to_utc_iso(av_ns),
                )
            )
        return ticks


class SyntheticResearchAdapter:
    """Deterministic research signal generator with fixed seed."""

    def __init__(self, seed: int = 20261127, strategy_id: str = "strat-meanrev-paper-v1") -> None:
        self._rng = random.Random(seed)
        self._strategy_id = strategy_id
        self._counter = 0

    def generate_signal(self, tick: MarketTick) -> ResearchSignal:
        self._counter += 1
        side = "BUY" if self._counter % 2 == 1 else "SELL"
        confidence = round(self._rng.uniform(0.62, 0.94), 4)
        qty = float(self._rng.choice([10, 20, 25, 50]))
        return ResearchSignal(
            signal_id=f"SIG-{self._counter:05d}",
            strategy_id=self._strategy_id,
            symbol=tick.symbol,
            side=side,
            confidence=confidence,
            suggested_qty=qty,
            reference_price=tick.mid_price,
            currency=tick.currency,
            feed_batch_id=tick.feed_batch_id,
            confidential_params={
                "strategy_params": {"alpha_decay": 0.91, "z_entry": 1.75},
                "alpha_weights": [0.42, 0.35, 0.23],
            },
        )


class PaperBrokerAdapter:
    """Deterministic paper/sandbox broker simulator with idempotency and injectable latency/reject/drift."""

    def __init__(
        self,
        seed: int = 20261127,
        venue: str = "PAPER_BROKER_1",
        faults: Optional[FaultInjectionProfile] = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._venue = venue
        self._faults = faults or FaultInjectionProfile()
        self._positions: Dict[str, float] = {}
        self._seen_client_orders: Dict[str, BrokerExecutionReport] = {}
        self._exec_counter = 0

    def submit_paper_order(
        self,
        order_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        currency: str,
    ) -> BrokerExecutionReport:
        # Idempotency: repeated client_order_id returns original execution report
        if client_order_id in self._seen_client_orders:
            return self._seen_client_orders[client_order_id]

        self._exec_counter += 1
        base_latency_ms = round(self._rng.uniform(4.0, 18.0), 3)
        latency_ms = base_latency_ms + self._faults.slow_broker_latency_ms

        if self._faults.force_broker_reject:
            report = BrokerExecutionReport(
                order_id=order_id,
                client_order_id=client_order_id,
                broker_exec_id=f"PEXEC-{self._exec_counter:06d}",
                venue=self._venue,
                symbol=symbol,
                side=side,
                status="rejected",
                filled_qty=0.0,
                avg_fill_price=0.0,
                currency=currency,
                ack_latency_ms=latency_ms,
                reject_reason="PAPER_RISK_OR_LIQUIDITY_REJECT",
            )
            self._seen_client_orders[client_order_id] = report
            return report

        signed_qty = qty if side == "BUY" else -qty
        broker_recorded_qty = signed_qty
        if self._faults.force_reconciliation_mismatch:
            broker_recorded_qty = signed_qty + 5.0  # injected fill/position drift
        self._positions[symbol] = self._positions.get(symbol, 0.0) + broker_recorded_qty

        report = BrokerExecutionReport(
            order_id=order_id,
            client_order_id=client_order_id,
            broker_exec_id=f"PEXEC-{self._exec_counter:06d}",
            venue=self._venue,
            symbol=symbol,
            side=side,
            status="filled",
            filled_qty=qty,
            avg_fill_price=limit_price,
            currency=currency,
            ack_latency_ms=latency_ms,
        )
        self._seen_client_orders[client_order_id] = report
        return report

    def fetch_broker_position_qty(self, symbol: str) -> float:
        return self._positions.get(symbol, 0.0)
