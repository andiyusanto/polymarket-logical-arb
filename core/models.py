"""Data models for the Polymarket Temporal & Conditional Arbitrage Bot.

Enums + dataclasses describing the signal flow:
  MarketInfo  → one tradeable token (YES side of a market) with live book
  MarketPair  → two markets with a confirmed logical dependency
  Cluster     → a group of related markets sharing a dependency family
  Violation   → a detected monotonicity / accounting-identity breach
  ShadowTrade → a Violation enriched with ambiguity scores + follow-up polls
  ArbTrade    → an executable (paper/live) bilateral order pair
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ViolationType(Enum):
    TEMPORAL_MONOTONICITY = "temporal"        # P(short deadline) > P(long deadline)
    THRESHOLD_MONOTONICITY = "threshold"      # P(high threshold) > P(low threshold)
    MUTUALLY_EXCLUSIVE = "mutual_exclusive"   # sum of outcome YES prices > 1.0


class ConfidenceTier(Enum):
    HIGH = "HIGH"        # structural + semantic both pass → full size
    MEDIUM = "MEDIUM"    # structural pass, semantic borderline → half size
    LOW = "LOW"          # flag for review, never traded


class ArbSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


# Execute violations in this priority order (empirical profit distribution:
# mutual-exclusive is the most proven type — $29M of $40M historical profit).
VIOLATION_PRIORITY = {
    ViolationType.MUTUALLY_EXCLUSIVE: 0,
    ViolationType.TEMPORAL_MONOTONICITY: 1,
    ViolationType.THRESHOLD_MONOTONICITY: 2,
}


@dataclass
class MarketInfo:
    """One tradeable outcome token (the YES side of a binary market).

    book_* fields are mirrored from LocalOrderBookCache so constraint checks
    can read prices without touching the network.
    """

    token_id: str                    # the YES outcome token
    question: str
    description: str = ""
    no_token_id: str = ""            # complementary NO token (for SELL-as-BUY-NO)
    end_date: Optional[datetime] = None
    volume_usd: float = 0.0
    condition_id: str = ""
    neg_risk: bool = False
    neg_risk_market_id: str = ""     # groups outcomes of one NegRisk event
    outcome: str = "Yes"             # the outcome this token represents
    taker_fee_rate: float = 0.0      # real per-market taker rate (feeSchedule.rate);
                                     # 0.0 = unknown → callers fall back to CFG default
    # Live book (mirrored from the cache; 0.0 until first WS/REST update)
    best_bid: float = 0.0
    best_ask: float = 0.0
    depth_usd: float = 0.0           # min(bid-side, ask-side) top-of-book USD
    book_updated: float = 0.0

    @property
    def mid(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2
        return self.best_ask or self.best_bid

    @property
    def implied_prob(self) -> float:
        """The market's implied probability of YES (mid price)."""
        return self.mid


@dataclass
class MarketPair:
    market_a: MarketInfo
    market_b: MarketInfo
    dependency_type: str             # "TEMPORAL" | "THRESHOLD" | "MUTUAL_EXCLUSIVE"
    llm_confidence: float
    cluster_id: str
    constraint: str = ""             # human-readable constraint statement
    reasoning: str = ""              # LLM justification (for cluster_review.log)


@dataclass
class Cluster:
    cluster_id: str
    markets: list                    # list[MarketInfo]
    dependency_type: str
    llm_confidence: float
    refreshed_at: float = 0.0

    def token_ids(self) -> list:
        return [m.token_id for m in self.markets]


@dataclass
class Violation:
    pair: MarketPair
    violation_type: ViolationType
    raw_spread_cents: float
    estimated_spread_after_slippage_cents: float
    leg_a_side: ArbSide              # action on pair.market_a
    leg_b_side: ArbSide             # action on pair.market_b
    detected_at: datetime
    # For mutual-exclusive violations the "pair" carries the two cheapest legs
    # but the breach is across the whole NegRisk set; sum_prob records the total.
    sum_prob: float = 0.0

    @property
    def priority(self) -> int:
        return VIOLATION_PRIORITY.get(self.violation_type, 99)


@dataclass
class ShadowTrade:
    violation: Violation
    structural_score: float
    semantic_score: float
    confidence_tier: ConfidenceTier
    intended_size_usdc: float
    book_snapshot_a: dict
    book_snapshot_b: dict
    reason: str = ""                 # why rejected if it didn't clear all gates
    detected_at: float = 0.0
    # Follow-up polls (populated async by the scheduler)
    spread_5s: Optional[float] = None
    spread_15s: Optional[float] = None
    spread_30s: Optional[float] = None
    spread_60s: Optional[float] = None
    still_valid_at_15s: Optional[bool] = None
    still_valid_at_30s: Optional[bool] = None
    # Live executable-side book depth (USD) captured at gate time — the side each
    # leg actually consumes (SELL→bids, BUY→asks). 0 = un-fillable on that side.
    book_depth_a: float = 0.0
    book_depth_b: float = 0.0


@dataclass
class ArbTrade:
    """An executable bilateral order pair (paper or live)."""

    id: str
    violation_type: str
    market_a_token: str
    market_b_token: str
    leg_a_side: str                  # "BUY" | "SELL"
    leg_b_side: str
    size_usdc: float
    entry_spread_cents: float
    confidence_tier: str
    neg_risk_a: bool = False
    neg_risk_b: bool = False
    mode: str = "PAPER"              # PAPER | LIVE
    status: str = "OPEN"            # OPEN | CLOSED | PARTIAL | UNWOUND
    pnl_usdc: float = 0.0
    detected_at: float = 0.0
    executed_at: float = 0.0
    notes: str = ""
    # Per-leg fill telemetry (populated by the executor)
    leg_a_fill_price: float = 0.0
    leg_b_fill_price: float = 0.0
    leg_a_filled: bool = False
    leg_b_filled: bool = False


@dataclass
class ArbResult:
    trade: ArbTrade
    success: bool
    unwound: bool = False
    error: str = ""
