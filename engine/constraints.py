"""Monotonicity / accounting-identity constraint checks.

Three independent checkers, each returning a list[Violation]. They read prices
from the dict the caller built off LocalOrderBookCache and model slippage by
walking the cached book via cache.estimate_fill_price — never the raw bid/ask
spread (Phase 3.1 requirement).

Spread convention (per share, in cents):
  A logical-arb leg is "SELL the overpriced market, BUY the underpriced one".
  realized spread = sell_fill(over) - buy_fill(under), then minus taker fees on
  both legs. We model the SELL leg as buying the complementary NO at execution
  time; at the YES-book level the captured edge is bid(over) - ask(under).

Violation directions:
  TEMPORAL   : P(earlier deadline) must be <= P(later deadline).
               Breach → SELL earlier, BUY later.
  THRESHOLD  : P(higher threshold) must be <= P(lower threshold).
               Breach → SELL higher, BUY lower.
  MUTUAL_EXCL: sum of YES prices across one NegRisk event must be <= 1.00.
               Breach (sum of bids > 1) → SELL the whole basket.
"""

import logging
import re
from datetime import datetime, timezone

from core.config import CFG
from core.models import ArbSide, MarketInfo, MarketPair, Violation, ViolationType

log = logging.getLogger("arb.constraints")

Prices = dict  # token_id -> (best_bid, best_ask)


# ─────────────────────────────────────────────────────────────────────
# Shared slippage / spread helper
# ─────────────────────────────────────────────────────────────────────
def _leg_spread_cents(
    over: MarketInfo,
    under: MarketInfo,
    prices: Prices,
    cache,
    size_usdc: float,
) -> tuple[float, float]:
    """(raw_cents, est_after_slippage_cents) for SELL `over` / BUY `under`."""
    bid_over = prices.get(over.token_id, (0.0, 0.0))[0]
    ask_under = prices.get(under.token_id, (0.0, 0.0))[1]
    raw = (bid_over - ask_under) * 100.0

    sell_fill = bid_over
    buy_fill = ask_under
    if cache is not None:
        s = cache.estimate_fill_price(over.token_id, "SELL", size_usdc)
        b = cache.estimate_fill_price(under.token_id, "BUY", size_usdc)
        if s is not None:
            sell_fill = s
        if b is not None:
            buy_fill = b
    fee = CFG.polymarket_taker_fee * (sell_fill + buy_fill) * 100.0
    est = (sell_fill - buy_fill) * 100.0 - fee
    return round(raw, 4), round(est, 4)


# ─────────────────────────────────────────────────────────────────────
# Date / threshold extraction
# ─────────────────────────────────────────────────────────────────────
def _extract_deadline(text: str):
    """Latest date mentioned in a question, as an aware datetime, or None.

    Uses dateparser (lazy import) per Phase 3.1 — not custom regex.
    """
    if not text:
        return None
    try:
        from dateparser.search import search_dates
    except ImportError:
        return None
    try:
        found = search_dates(
            text, settings={"RETURN_AS_TIMEZONE_AWARE": True, "PREFER_DATES_FROM": "future"}
        )
    except Exception:
        return None
    if not found:
        return None
    dates = []
    for _txt, dt in found:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dates.append(dt)
    # The deadline is the latest date referenced in the question text.
    return max(dates) if dates else None


# Capture whether a "$" prefix was present and whether a magnitude suffix
# (k/m/b) attaches directly (no space, not followed by another letter — so
# "$90,000 by Dec" doesn't read the 'b' in "by" as billions).
_THRESH_RE = re.compile(
    r"(\$)?\s*([0-9][0-9,]*\.?[0-9]*)([kKmMbB])?(?![A-Za-z])",
)


def _extract_threshold(text: str):
    """The threshold value in a question, or None.

    Handles $100k, $100,000, 100000, $1M, 1.5B, etc. A number marked as a price
    ($-prefixed OR k/m/b-suffixed) always wins over a bare number, so a 4-digit
    year ("by 2026") never masquerades as the threshold. If nothing is marked as
    a price, fall back to the largest bare number that isn't a plausible year.
    """
    if not text:
        return None
    priced: list[float] = []
    bare: list[float] = []
    for m in _THRESH_RE.finditer(text):
        has_dollar, num_s, suffix = m.group(1), m.group(2), m.group(3)
        try:
            val = float(num_s.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            val *= {"k": 1e3, "m": 1e6, "b": 1e9}[suffix.lower()]
        if has_dollar or suffix:
            priced.append(val)
        elif not (1900 <= val <= 2100):   # skip plausible calendar years
            bare.append(val)
    if priced:
        return max(priced)
    return max(bare) if bare else None


def _prices_ok(*tokens, prices: Prices) -> bool:
    for t in tokens:
        bid, ask = prices.get(t, (0.0, 0.0))
        if bid <= 0 or ask <= 0:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────
# 1) Temporal monotonicity
# ─────────────────────────────────────────────────────────────────────
def check_temporal_monotonicity(
    cluster: list[MarketInfo],
    prices: Prices,
    cache=None,
    size_usdc: float = None,
) -> list[Violation]:
    size_usdc = size_usdc or CFG.base_position_usdc
    dated = []
    for m in cluster:
        dl = m.end_date or _extract_deadline(m.question)
        if dl is not None:
            dated.append((dl, m))
    if len(dated) < 2:
        return []
    dated.sort(key=lambda x: x[0])

    violations: list[Violation] = []
    noise = CFG.noise_threshold_cents
    for i in range(len(dated)):
        for j in range(i + 1, len(dated)):
            (_d_early, early), (_d_late, late) = dated[i], dated[j]
            if early.end_date == late.end_date:
                continue
            if not _prices_ok(early.token_id, late.token_id, prices=prices):
                continue
            p_early = prices[early.token_id][0]  # bid (we'd sell here)
            p_late = prices[late.token_id][1]    # ask (we'd buy here)
            raw = (p_early - p_late) * 100.0
            if raw <= noise:
                continue
            raw_c, est_c = _leg_spread_cents(early, late, prices, cache, size_usdc)
            violations.append(
                Violation(
                    pair=MarketPair(
                        market_a=early,
                        market_b=late,
                        dependency_type="TEMPORAL",
                        llm_confidence=0.0,
                        cluster_id="",
                        constraint="P(earlier deadline) <= P(later deadline)",
                    ),
                    violation_type=ViolationType.TEMPORAL_MONOTONICITY,
                    raw_spread_cents=raw_c,
                    estimated_spread_after_slippage_cents=est_c,
                    leg_a_side=ArbSide.SELL,   # earlier (overpriced)
                    leg_b_side=ArbSide.BUY,    # later (underpriced)
                    detected_at=datetime.now(tz=timezone.utc),
                )
            )
    return violations


# ─────────────────────────────────────────────────────────────────────
# 2) Threshold monotonicity
# ─────────────────────────────────────────────────────────────────────
def check_threshold_monotonicity(
    cluster: list[MarketInfo],
    prices: Prices,
    cache=None,
    size_usdc: float = None,
) -> list[Violation]:
    size_usdc = size_usdc or CFG.base_position_usdc
    leveled = []
    for m in cluster:
        thr = _extract_threshold(m.question)
        if thr is not None:
            leveled.append((thr, m))
    if len(leveled) < 2:
        return []
    leveled.sort(key=lambda x: x[0])  # ascending threshold

    violations: list[Violation] = []
    noise = CFG.noise_threshold_cents
    for i in range(len(leveled)):
        for j in range(i + 1, len(leveled)):
            (t_low, low), (t_high, high) = leveled[i], leveled[j]
            if t_low == t_high:
                continue
            if not _prices_ok(low.token_id, high.token_id, prices=prices):
                continue
            p_high = prices[high.token_id][0]  # bid (sell the higher threshold)
            p_low = prices[low.token_id][1]    # ask (buy the lower threshold)
            raw = (p_high - p_low) * 100.0
            if raw <= noise:
                continue
            raw_c, est_c = _leg_spread_cents(high, low, prices, cache, size_usdc)
            violations.append(
                Violation(
                    pair=MarketPair(
                        market_a=high,
                        market_b=low,
                        dependency_type="THRESHOLD",
                        llm_confidence=0.0,
                        cluster_id="",
                        constraint="P(higher threshold) <= P(lower threshold)",
                    ),
                    violation_type=ViolationType.THRESHOLD_MONOTONICITY,
                    raw_spread_cents=raw_c,
                    estimated_spread_after_slippage_cents=est_c,
                    leg_a_side=ArbSide.SELL,   # higher threshold (overpriced)
                    leg_b_side=ArbSide.BUY,    # lower threshold (underpriced)
                    detected_at=datetime.now(tz=timezone.utc),
                )
            )
    return violations


# ─────────────────────────────────────────────────────────────────────
# 3) Mutually exclusive (NegRisk sum)
# ─────────────────────────────────────────────────────────────────────
def check_mutually_exclusive(
    cluster: list[MarketInfo],
    prices: Prices,
    cache=None,
    size_usdc: float = None,
) -> list[Violation]:
    size_usdc = size_usdc or CFG.base_position_usdc
    legs = [m for m in cluster if prices.get(m.token_id, (0.0, 0.0))[0] > 0]
    if len(legs) < 2:
        return []

    # Sellable basket: sum of best bids > 1 means we can sell every YES for more
    # than the $1 we'll owe when exactly one outcome resolves YES.
    sum_bids = sum(prices[m.token_id][0] for m in legs)
    raw_cents = (sum_bids - 1.0) * 100.0
    if raw_cents <= CFG.noise_threshold_cents:
        return []

    # After-slippage: per-leg sell fill across the basket, minus taker fees.
    per_leg = size_usdc / len(legs)
    sell_fills = []
    for m in legs:
        f = None
        if cache is not None:
            f = cache.estimate_fill_price(m.token_id, "SELL", per_leg)
        sell_fills.append(f if f is not None else prices[m.token_id][0])
    sum_fill = sum(sell_fills)
    fee = CFG.polymarket_taker_fee * sum_fill * 100.0
    est_cents = (sum_fill - 1.0) * 100.0 - fee

    # Represent the basket via its two richest legs for the pair model.
    ranked = sorted(legs, key=lambda m: prices[m.token_id][0], reverse=True)
    a, b = ranked[0], ranked[1]
    return [
        Violation(
            pair=MarketPair(
                market_a=a,
                market_b=b,
                dependency_type="MUTUAL_EXCLUSIVE",
                llm_confidence=1.0,
                cluster_id="",
                constraint=f"sum(YES) <= 1.00 across {len(legs)} outcomes",
            ),
            violation_type=ViolationType.MUTUALLY_EXCLUSIVE,
            raw_spread_cents=round(raw_cents, 4),
            estimated_spread_after_slippage_cents=round(est_cents, 4),
            leg_a_side=ArbSide.SELL,
            leg_b_side=ArbSide.SELL,
            detected_at=datetime.now(tz=timezone.utc),
            sum_prob=round(sum_bids, 4),
        )
    ]
