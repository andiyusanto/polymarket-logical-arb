"""Paper-mode bilateral execution + the base Executor that SdkExecutor extends.

A logical-arb position is two (or, for a mutually-exclusive basket, several)
legs that LOCK IN the spread at entry. Canonical executable form: every leg is a
BUY.
  - a "BUY" leg buys the underpriced YES token
  - a "SELL" leg buys the complementary NO token (selling YES == buying NO)
So the executor never needs to short or pre-hold inventory.

Paper mode simulates both legs filling at the current cached book prices (slippage
via LocalOrderBookCache.estimate_fill_price) and books the captured spread as
realized PnL. The bilateral-submission and partial-fill/unwind machinery lives in
SdkExecutor (live) — paper assumes atomic fill since G5 already required depth.
"""

import logging
import time
import uuid
from typing import Optional

from core.config import CFG
from core.database import Database
from core.models import ArbResult, ArbSide, ArbTrade, ShadowTrade, ViolationType

log = logging.getLogger("arb.executor")


def resolve_leg(market, side: ArbSide) -> tuple[str, str]:
    """(token_id, action) for a leg — always a BUY, on YES or the complement NO."""
    if side == ArbSide.BUY:
        return market.token_id, "BUY"
    return (market.no_token_id or market.token_id), "BUY"


class Executor:
    def __init__(self, db: Database, cache, is_live: bool = False) -> None:
        self.db = db
        self.cache = cache
        self.is_live = is_live
        self.open_count = 0

    # ── Public entry point (paper); SdkExecutor overrides for live ────
    async def execute_arb(self, st: ShadowTrade) -> ArbResult:
        return self.execute_arb_paper(st)

    def execute_arb_paper(self, st: ShadowTrade) -> ArbResult:
        v = st.violation
        a, b = v.pair.market_a, v.pair.market_b
        size = st.intended_size_usdc or CFG.base_position_usdc

        # Simulate each leg's fill from the cached YES book using the constraint
        # convention (SELL → walk bids, BUY → walk asks) — the same prices the
        # spread was computed from. The SELL-as-BUY-NO translation is a live
        # order-placement detail (resolve_leg); paper prices the YES book.
        fill_a = self.cache.estimate_fill_price(a.token_id, v.leg_a_side.value, size)
        fill_b = self.cache.estimate_fill_price(b.token_id, v.leg_b_side.value, size)

        trade = self._build_trade(st, "PAPER")
        if fill_a is None or fill_b is None:
            trade.status = "CANCELLED"
            trade.notes = "paper: empty book on a leg"
            self.db.save_trade(trade)
            return ArbResult(trade=trade, success=False, error="empty book")

        trade.leg_a_fill_price = round(fill_a, 4)
        trade.leg_b_fill_price = round(fill_b, 4)
        trade.leg_a_filled = trade.leg_b_filled = True

        # Captured spread (cents) was computed at detection; realize it as PnL.
        spread_cents = v.estimated_spread_after_slippage_cents
        price_ref = self._price_ref(v, fill_a, fill_b)
        shares = size / price_ref if price_ref > 0 else 0.0
        trade.pnl_usdc = round(spread_cents / 100.0 * shares, 6)
        trade.status = "CLOSED"
        trade.executed_at = time.time()
        self.db.save_trade(trade)
        log.info(
            "PAPER ARB %s: %.2f¢ size=$%.0f legs[%s/%s] pnl=$%+.4f",
            v.violation_type.value, spread_cents, size,
            v.leg_a_side.value, v.leg_b_side.value, trade.pnl_usdc,
        )
        return ArbResult(trade=trade, success=True)

    # ── Shared trade builder ──────────────────────────────────────────
    def _build_trade(self, st: ShadowTrade, mode: str) -> ArbTrade:
        v = st.violation
        return ArbTrade(
            id=f"ARB-{uuid.uuid4().hex[:10]}",
            violation_type=v.violation_type.value,
            market_a_token=v.pair.market_a.token_id,
            market_b_token=v.pair.market_b.token_id,
            leg_a_side=v.leg_a_side.value,
            leg_b_side=v.leg_b_side.value,
            size_usdc=st.intended_size_usdc or CFG.base_position_usdc,
            entry_spread_cents=v.estimated_spread_after_slippage_cents,
            confidence_tier=st.confidence_tier.value,
            neg_risk_a=v.pair.market_a.neg_risk,
            neg_risk_b=v.pair.market_b.neg_risk,
            mode=mode,
            status="OPEN",
            detected_at=st.detected_at or time.time(),
        )

    @staticmethod
    def _price_ref(v, fill_a: float, fill_b: float) -> float:
        # Reference price for the share-count estimate.
        if v.violation_type == ViolationType.MUTUALLY_EXCLUSIVE:
            return max(fill_a, fill_b, 0.01)
        return max(fill_b, 0.01)   # the BUY (underpriced) leg
