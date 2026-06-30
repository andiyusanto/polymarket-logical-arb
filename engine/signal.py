"""ArbEngine: constraint checks + ambiguity gates → ShadowTrade signals.

evaluate_cluster() is the hot path, called whenever a book update arrives for any
token in a cluster. It runs the constraint check matching the cluster's
dependency type, then pushes every violation through the gate ladder:

    G1  cluster has a confirmed LLM dependency (>= llm_confidence_threshold)
    G6  current UTC hour not in blackout_hours          (cheap)
    G7  active concurrent arbs < max_concurrent_arbs     (cheap)
    G4  estimated spread after slippage >= min spread    (cheap)
    G5  book depth >= min on BOTH legs                    (cheap)
    G2  structural_score >= threshold                     (cheap; skipped for ME)
    G3  semantic_score tier HIGH/MEDIUM                   (LLM; skipped for ME)

Cheap gates run before the LLM gate so the expensive semantic call only fires on
otherwise-qualified violations. The first failing gate is recorded as the reason;
a violation clearing all gates is reason="PASS". Mutually-exclusive violations
skip G2/G3 (accounting identity — CLAUDE.md gate philosophy).

Follow-up polls (5/15/30/60s) re-measure the spread from the cache so shadow data
captures the decay curve and spread duration.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from core.config import CFG
from core.models import (
    ConfidenceTier,
    ShadowTrade,
    Violation,
    ViolationType,
)
from engine import ambiguity
from engine.constraints import (
    _leg_spread_cents,
    check_mutually_exclusive,
    check_temporal_monotonicity,
    check_threshold_monotonicity,
)

log = logging.getLogger("arb.engine")

_CHECK_BY_TYPE = {
    "MUTUAL_EXCLUSIVE": check_mutually_exclusive,
    "TEMPORAL": check_temporal_monotonicity,
    "THRESHOLD": check_threshold_monotonicity,
}


class ArbEngine:
    def __init__(self, cache, cluster_map, shadow_logger=None, db=None) -> None:
        self.cache = cache
        self.cluster_map = cluster_map
        self.shadow_logger = shadow_logger
        self.db = db
        # Per-pair ambiguity result cache so the LLM isn't re-queried on every
        # book tick. Keyed by frozenset of token ids. (structural, semantic, tier)
        self._ambiguity_cache: dict[frozenset, tuple] = {}
        self._followup_tasks: set = set()

    # ── Hot path ─────────────────────────────────────────────────────
    async def evaluate_cluster(self, cluster) -> list[ShadowTrade]:
        if cluster is None or len(cluster.markets) < 2:
            return []

        prices = self._prices_for(cluster.markets)
        checker = _CHECK_BY_TYPE.get(cluster.dependency_type)
        if checker is None:
            return []
        violations = checker(cluster.markets, prices, self.cache)

        # Highest-priority violation type first (ME > TEMPORAL > THRESHOLD).
        violations.sort(key=lambda v: v.priority)

        out: list[ShadowTrade] = []
        for v in violations:
            v.pair.cluster_id = cluster.cluster_id
            v.pair.llm_confidence = cluster.llm_confidence
            st = await self._gate(v, cluster)
            out.append(st)
            self._emit(st)
            if st.reason == "PASS" or (
                v.estimated_spread_after_slippage_cents
                >= CFG.min_violation_spread_cents
            ):
                self._schedule_followups(st)
        return out

    # ── Gate ladder ──────────────────────────────────────────────────
    async def _gate(self, v: Violation, cluster) -> ShadowTrade:
        is_me = v.violation_type == ViolationType.MUTUALLY_EXCLUSIVE
        # Executable-side depth, captured once: each leg consumes the side it
        # trades (SELL→bids, BUY→asks). A one-sided book stays fillable on its
        # live side — this never collapses to 0 the way depth()'s min() did.
        da = self.cache.side_depth(v.pair.market_a.token_id, v.leg_a_side.value)
        dbk = self.cache.side_depth(v.pair.market_b.token_id, v.leg_b_side.value)

        def trade(struct, sem, tier, reason) -> ShadowTrade:
            mult = (
                CFG.position_multiplier_high
                if tier == ConfidenceTier.HIGH
                else CFG.position_multiplier_med
                if tier == ConfidenceTier.MEDIUM
                else 0.0
            )
            st = ShadowTrade(
                violation=v,
                structural_score=struct,
                semantic_score=sem,
                confidence_tier=tier,
                intended_size_usdc=CFG.base_position_usdc * mult,
                book_snapshot_a=self.cache.book_snapshot(v.pair.market_a.token_id),
                book_snapshot_b=self.cache.book_snapshot(v.pair.market_b.token_id),
                reason=reason,
                detected_at=time.time(),
            )
            st.book_depth_a = da
            st.book_depth_b = dbk
            st.shadow_uid = uuid.uuid4().hex
            return st

        # G1: confirmed dependency
        if cluster.llm_confidence < CFG.llm_confidence_threshold:
            return trade(0.0, 0.0, ConfidenceTier.LOW, "G1_NO_CLUSTER")

        # G6: blackout hour
        hour = datetime.now(tz=timezone.utc).hour
        if hour in CFG.blackout_hours:
            return trade(0.0, 0.0, ConfidenceTier.LOW, "G6_BLACKOUT")

        # G7: concurrency limit
        active = self.db.active_arb_count() if self.db else 0
        if active >= CFG.max_concurrent_arbs:
            return trade(0.0, 0.0, ConfidenceTier.LOW, "G7_MAX_CONCURRENT")

        # G4: spread after slippage
        if v.estimated_spread_after_slippage_cents < CFG.min_violation_spread_cents:
            return trade(0.0, 0.0, ConfidenceTier.LOW, "G4_SPREAD_TOO_SMALL")

        # G5: executable-side depth on both legs (da/dbk computed above)
        if da < CFG.min_book_depth_usd or dbk < CFG.min_book_depth_usd:
            return trade(0.0, 0.0, ConfidenceTier.LOW, "G5_ILLIQUID")

        # Mutually-exclusive skips the NLP gates entirely.
        if is_me:
            return trade(1.0, 1.0, ConfidenceTier.HIGH, "PASS")

        # G2 + G3: ambiguity (cached per pair)
        struct, sem, tier = await self._ambiguity(v)
        if struct < CFG.structural_score_threshold:
            return trade(struct, sem, ConfidenceTier.LOW, "G2_STRUCTURAL")
        if tier == ConfidenceTier.LOW:
            return trade(struct, sem, ConfidenceTier.LOW, "G3_SEMANTIC")
        return trade(struct, sem, tier, "PASS")

    async def _ambiguity(self, v: Violation):
        a, b = v.pair.market_a, v.pair.market_b
        key = frozenset((a.token_id, b.token_id))
        cached = self._ambiguity_cache.get(key)
        if cached is not None:
            return cached

        struct = ambiguity.structural_score(a.question, b.question)
        sem = 0.0
        tier = ConfidenceTier.LOW
        if struct >= CFG.structural_score_threshold:
            sem = await ambiguity.semantic_score(
                a.question, a.description, b.question, b.description,
                v.pair.dependency_type,
            )
            if sem >= CFG.semantic_score_threshold:
                tier = ConfidenceTier.HIGH
            elif sem >= 0.70:
                tier = ConfidenceTier.MEDIUM
            else:
                tier = ConfidenceTier.LOW
        result = (struct, sem, tier)
        self._ambiguity_cache[key] = result
        return result

    # ── Follow-up polls ──────────────────────────────────────────────
    def _schedule_followups(self, st: ShadowTrade) -> None:
        # Follow-up polls only feed shadow telemetry; skip them in bot mode.
        if self.shadow_logger is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._followup(st))
        self._followup_tasks.add(task)
        task.add_done_callback(self._followup_tasks.discard)

    async def _followup(self, st: ShadowTrade) -> None:
        prev = 0.0
        for mark in CFG.followup_poll_secs:
            await asyncio.sleep(mark - prev)   # incremental delta to each poll point
            prev = mark
            est = self._recompute_spread(st.violation)
            still_valid = est is not None and est >= CFG.min_violation_spread_cents
            if mark == 5.0:
                st.spread_5s = est
            elif mark == 15.0:
                st.spread_15s = est
                st.still_valid_at_15s = still_valid
            elif mark == 30.0:
                st.spread_30s = est
                st.still_valid_at_30s = still_valid
            elif mark == 60.0:
                st.spread_60s = est
            self._emit(st)  # REPLACE the row with updated follow-up fields

    def _recompute_spread(self, v: Violation):
        prices = self._prices_for([v.pair.market_a, v.pair.market_b])
        if v.violation_type == ViolationType.MUTUALLY_EXCLUSIVE:
            cluster = self.cluster_map.get_cluster_for(v.pair.market_a.token_id)
            members = cluster.markets if cluster else [v.pair.market_a, v.pair.market_b]
            prices = self._prices_for(members)
            res = check_mutually_exclusive(members, prices, self.cache)
            return res[0].estimated_spread_after_slippage_cents if res else None
        _raw, est = _leg_spread_cents(
            v.pair.market_a, v.pair.market_b, prices, self.cache,
            CFG.base_position_usdc,
        )
        return est

    # ── Helpers ──────────────────────────────────────────────────────
    def _prices_for(self, markets) -> dict:
        out = {}
        for m in markets:
            bid, ask = self.cache.get_best_bid_ask(m.token_id)
            out[m.token_id] = (bid, ask)
        return out

    def _emit(self, st: ShadowTrade) -> None:
        if self.shadow_logger is not None:
            self.shadow_logger.record(st)
