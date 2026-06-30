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


# A threshold market's monotonicity DIRECTION decides which leg is the rarer
# (subset) event:
#   UP   ("exceed/hit/above $X"): a HIGHER level is harder → rarer/less likely.
#   DOWN ("dip to/fall below $X"): a LOWER level is harder → to reach $45k price
#         must pass $55k, so the higher level is MORE likely — inverted from up.
# Treating a downward market as upward turns a correct, no-arb price ordering into
# a phantom violation (the BTC/ETH "dip to" false PASSes in shadow). The DOWN
# matcher is deliberately broad and wins over UP; an unclassifiable market returns
# None and is skipped — we never trade a guessed direction (fail closed).
_DOWN_RE = re.compile(
    r"\b(?:dips?|dipping|falls?|falling|drops?|dropping|declines?|declining|"
    r"sinks?|sinking|slumps?|plunges?|plunging|tumbles?|tumbling|crash(?:es)?|"
    r"crashing|below|under|beneath|less\s+than|lower\s+than|down\s+to)\b",
    re.IGNORECASE,
)
_UP_RE = re.compile(
    r"\b(?:exceeds?|exceeding|above|over|more\s+than|greater\s+than|at\s+least|"
    r"reach(?:es)?|reaching|hits?|hitting|surpass(?:es)?|surpassing|climbs?|"
    r"climbing|rises?|rising|up\s+to)\b",
    re.IGNORECASE,
)


def _threshold_direction(text: str):
    """'up', 'down', or None (ambiguous → skip, fail closed)."""
    if not text:
        return None
    if _DOWN_RE.search(text):
        return "down"
    if _UP_RE.search(text):
        return "up"
    return None


def _prices_ok(*tokens, prices: Prices) -> bool:
    for t in tokens:
        bid, ask = prices.get(t, (0.0, 0.0))
        if bid <= 0 or ask <= 0:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────
# 1) Temporal monotonicity
# ─────────────────────────────────────────────────────────────────────
# Temporal monotonicity also has a DIRECTION:
#   "by"    — achievement by a deadline ("X happens by Dec"): monotone
#             INCREASING in the date (more time → more likely), so the EARLIER
#             deadline is the rarer (subset) event. The default and the
#             overwhelming majority of temporal markets.
#   "until" — persistence/negation ("X stays below $Y until Dec", "X does NOT
#             reach $Y by Dec"): monotone DECREASING (more time → harder), so the
#             LATER deadline is the rarer event — inverted legs.
# Only an explicit persistence/negation marker flips a market to "until";
# everything else defaults to "by" (preserving prior behaviour). Bare "through"
# and "hold" are intentionally excluded — they collide with achievement phrases
# ("pass through", "hold elections"). Mis-handling a persistence market as
# achievement legs the trade backwards (same bug class as threshold "dip to").
_UNTIL_RE = re.compile(
    r"\b(?:remains?|remaining|stays?|staying|persists?|persisting|throughout|"
    r"until|avoids?|"
    # negation of ANY achievement verb inverts monotonicity ("not reach", "fail
    # to qualify", "never confirmed"); require whitespace after the marker so
    # hyphenated "not-for-profit" is not misread as a negation.
    r"(?:not|won'?t|cannot|can'?t|never|fails?\s+to|unable\s+to|doesn'?t|"
    r"does\s+not)\s+\w+"
    r")\b",
    re.IGNORECASE,
)


def _temporal_direction(text: str) -> str:
    """'until' (persistence/negation, decreasing) or 'by' (achievement,
    increasing — the default). Never None: an unmarked market is the common 'by'
    framing, which preserves prior behaviour."""
    return "until" if text and _UNTIL_RE.search(text) else "by"


def check_temporal_monotonicity(
    cluster: list[MarketInfo],
    prices: Prices,
    cache=None,
    size_usdc: float = None,
) -> list[Violation]:
    size_usdc = size_usdc or CFG.base_position_usdc
    # (deadline, direction, market). Compare only same-direction markets — a
    # "happens by" and a "remains until" are not a monotone pair.
    dated = []
    for m in cluster:
        dl = m.end_date or _extract_deadline(m.question)
        if dl is None:
            continue
        dated.append((dl, _temporal_direction(m.question), m))
    if len(dated) < 2:
        return []

    violations: list[Violation] = []
    noise = CFG.noise_threshold_cents
    for direction in ("by", "until"):
        group = sorted(
            ((dl, m) for dl, d, m in dated if d == direction),
            key=lambda x: x[0],          # ascending deadline (earliest first)
        )
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                (d_early, m_early), (d_late, m_late) = group[i], group[j]
                if d_early == d_late:    # same deadline → no monotone edge
                    continue
                # rarer (subset) leg vs more-likely (superset) leg:
                #   by    : earlier deadline is rarer → subset = m_early
                #   until : later   deadline is rarer → subset = m_late
                if direction == "by":
                    subset, superset = m_early, m_late
                else:
                    subset, superset = m_late, m_early
                if not _prices_ok(subset.token_id, superset.token_id, prices=prices):
                    continue
                p_sub = prices[subset.token_id][0]    # bid (SELL the rarer leg)
                p_sup = prices[superset.token_id][1]  # ask (BUY the likelier leg)
                raw = (p_sub - p_sup) * 100.0
                if raw <= noise:
                    continue
                raw_c, est_c = _leg_spread_cents(
                    subset, superset, prices, cache, size_usdc
                )
                violations.append(
                    Violation(
                        pair=MarketPair(
                            market_a=subset,
                            market_b=superset,
                            dependency_type="TEMPORAL",
                            llm_confidence=0.0,
                            cluster_id="",
                            constraint=(
                                "P(rarer deadline) <= P(likelier deadline) "
                                f"[{direction}]"
                            ),
                        ),
                        violation_type=ViolationType.TEMPORAL_MONOTONICITY,
                        raw_spread_cents=raw_c,
                        estimated_spread_after_slippage_cents=est_c,
                        leg_a_side=ArbSide.SELL,   # rarer leg (overpriced)
                        leg_b_side=ArbSide.BUY,    # likelier leg (underpriced)
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
    # (threshold, direction, market). Skip a market with no threshold OR no
    # classifiable direction — we never leg a market on a guessed direction.
    leveled = []
    for m in cluster:
        thr = _extract_threshold(m.question)
        if thr is None:
            continue
        direction = _threshold_direction(m.question)
        if direction is None:
            continue
        leveled.append((thr, direction, m))
    if len(leveled) < 2:
        return []

    violations: list[Violation] = []
    noise = CFG.noise_threshold_cents
    # Only compare markets that share a direction: an "exceed $X" and a
    # "dip to $Y" are not a monotone pair and must never be legged together.
    for direction in ("up", "down"):
        group = sorted(
            ((t, m) for t, d, m in leveled if d == direction),
            key=lambda x: x[0],          # ascending dollar level
        )
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                (t_lo, m_lo), (t_hi, m_hi) = group[i], group[j]
                if t_lo == t_hi:
                    continue
                # Identify the rarer (subset) leg vs the more-likely (superset)
                # leg from the direction. The invariant is always
                # P(rarer) <= P(more-likely); a breach means the rarer leg is
                # priced ABOVE the likelier one → SELL rarer, BUY likelier.
                #   up   : higher $ is rarer → subset = m_hi, superset = m_lo
                #   down : lower  $ is rarer → subset = m_lo, superset = m_hi
                if direction == "up":
                    subset, superset = m_hi, m_lo
                else:
                    subset, superset = m_lo, m_hi
                if not _prices_ok(subset.token_id, superset.token_id, prices=prices):
                    continue
                p_sub = prices[subset.token_id][0]    # bid (SELL the rarer leg)
                p_sup = prices[superset.token_id][1]  # ask (BUY the likelier leg)
                raw = (p_sub - p_sup) * 100.0
                if raw <= noise:
                    continue
                raw_c, est_c = _leg_spread_cents(
                    subset, superset, prices, cache, size_usdc
                )
                violations.append(
                    Violation(
                        pair=MarketPair(
                            market_a=subset,
                            market_b=superset,
                            dependency_type="THRESHOLD",
                            llm_confidence=0.0,
                            cluster_id="",
                            constraint=(
                                "P(rarer threshold) <= P(likelier threshold) "
                                f"[{direction}ward]"
                            ),
                        ),
                        violation_type=ViolationType.THRESHOLD_MONOTONICITY,
                        raw_spread_cents=raw_c,
                        estimated_spread_after_slippage_cents=est_c,
                        leg_a_side=ArbSide.SELL,   # rarer leg (overpriced)
                        leg_b_side=ArbSide.BUY,    # likelier leg (underpriced)
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
