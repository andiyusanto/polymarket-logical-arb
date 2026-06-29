# CLAUDE.md — Polymarket Temporal & Conditional Arbitrage Bot
# repo: polymarket-logical-arb

This file is the authoritative development rulebook for this project.
Read it fully before making any change to code, config, or thresholds.
When in doubt, read this file again.

---

## What This Bot Does

This bot detects and trades **logical arbitrage** across related Polymarket
prediction markets. It enforces mathematical probability constraints:

- **Temporal monotonicity**: P("X by Sept") ≤ P("X by Dec") — earlier deadline
  cannot be more likely than later deadline for the same event
- **Threshold monotonicity**: P("BTC > $100k") ≤ P("BTC > $90k") — higher
  threshold cannot be more likely than lower threshold
- **Mutually exclusive**: Sum of all outcome probabilities in one event ≤ 1.00

When these constraints are violated AND the logical relationship between markets
is genuine AND both markets will resolve consistently, a risk-free arbitrage
exists.

**This is NOT a directional bet. This is NOT correlation trading.**
The edge comes from logical necessity, not probability estimation.

---

## What This Bot Is NOT

- NOT a speed-based arbitrage bot — execution window is seconds, not milliseconds
- NOT a single-market YES+NO rebalancing bot (that's dominated by institutional MMs)
- NOT a directional oracle sniper (that's the bear bot — different strategy entirely)
- NOT a statistical arbitrage bot (correlation can break — logical constraints cannot)

---

## Reference Implementation

The bear bot at `~/polymarket-arbitrage-bot/bear-oracle-confirmed-sniper` is the reference for:
- SDK integration and signing (`execution/sdk_executor.py`)
- Shadow mode architecture (`shadow.py`, `core/shadow.py`)
- Gate philosophy and development rules (its `CLAUDE.md`)
- WebSocket management (`feeds/prices.py`, `feeds/markets.py`)
- Analysis tooling pattern (`analysis/`)

**Never modify the bear bot repo.** It is read-only reference.
If you need to reuse code, copy it and adapt — do not import from it.

The arb bot lives at `~/polymarket-arbitrage-bot/polymarket-logical-arb/`.

---

## Architecture: How Signal Flows

```
WebSocket book update (token_id)
        ↓
LocalOrderBookCache.update(token_id, book)
        ↓
cluster_map.get_cluster_for(token_id) → cluster
        ↓
engine.evaluate_cluster(cluster)
    ├── constraints.check_temporal_monotonicity()
    ├── constraints.check_threshold_monotonicity()
    └── constraints.check_mutually_exclusive()   ← highest priority
        ↓
For each Violation:
    ├── ambiguity.structural_score() → reject if < threshold
    ├── ambiguity.semantic_score() → determine ConfidenceTier
    ├── estimate_fill_price() → compute spread after slippage
    └── reject if spread < CFG.min_violation_spread_cents
        ↓
ShadowTrade → ShadowLogger (shadow mode)
         OR
ArbTrade → executor.execute_arb() (paper/live mode)
```

---

## Gate Philosophy

Every violation must pass ALL gates before execution:

| Gate | Check | Reject if |
|------|-------|-----------|
| G1 | Cluster has ≥2 markets with confirmed LLM dependency | No valid cluster |
| G2 | structural_score ≥ CFG.structural_score_threshold | Wording trap detected |
| G3 | semantic_score ≥ CFG.semantic_score_threshold (HIGH/MED) | Ambiguous resolution |
| G4 | estimated_spread_after_slippage ≥ CFG.min_violation_spread_cents | Edge eaten by slippage |
| G5 | Book depth ≥ $20 on both legs | Too illiquid to fill |
| G6 | Not in CFG.blackout_hours (UTC) | Historical low-quality period |
| G7 | Active concurrent arbs < CFG.max_concurrent_arbs | Risk exposure limit |

**Mutual exclusive violations skip G2/G3** — they don't require NLP analysis
because the math is pure accounting identity within one NegRisk market.

---

## Violation Priority

Execute violations in this priority order (based on empirical profit distribution):

1. **MUTUALLY_EXCLUSIVE** — most proven, $29M of $40M historical profit,
   zero ambiguity risk (same event, same resolution)
2. **TEMPORAL_MONOTONICITY** — logical necessity, but requires wording analysis
3. **THRESHOLD_MONOTONICITY** — valid but rarest, number extraction tricky

When multiple violations exist simultaneously, execute highest priority first.
Do NOT execute more than CFG.max_concurrent_arbs simultaneously.

---

## The Critical Execution Rule

**BOTH LEGS MUST BE SUBMITTED SIMULTANEOUSLY via asyncio.gather().**

This is non-negotiable. Sequential submission introduces legging risk that
defeats the entire purpose of logical arbitrage.

```python
# CORRECT
results = await asyncio.gather(
    place_order(leg_a),
    place_order(leg_b),
    return_exceptions=True
)

# WRONG — never do this
result_a = await place_order(leg_a)
result_b = await place_order(leg_b)  # leg_a may have moved the market
```

**Partial fill protocol:**
- If leg_a fills and leg_b fails → unwind leg_a immediately (within 30s max)
- Unwind = place opposite market order on leg_a at current book price
- Log as UNWOUND trade, not as a loss
- Unwind rate > 15% over 20 trades = systemic problem, investigate before continuing

---

## Development Rules

### Rule 1: Shadow Mode First, Always

No code change that affects signal detection, constraint logic, ambiguity
scoring, or position sizing goes to paper/live without shadow mode verification.

Minimum shadow run after any logic change: **48 hours**.
Minimum shadow run before initial go-live: **14 days**.

### Rule 2: Config Changes Require Justification

Every change to a threshold in `core/config.py` must be accompanied by:
1. The analysis output that justifies the change
2. The specific metric that was out of range
3. The expected effect of the change

Do NOT change thresholds based on intuition. Change them based on
`analysis/shadow_report.py` or `analysis/optimize.py` output only.

Log every config change in this file under ## Config Change Log.

### Rule 3: Go-Live Gate Is Not Negotiable

`analysis/optimize.py --min-days 14` must output `→ GO LIVE` before
any live capital is deployed. If it outputs `→ NO-GO`, fix the root
cause first.

The reference thresholds (not hard limits — these come from empirical research
on analogous markets):

```
golive_min_spread_cents = 2.0    # ≥2¢ realized spread after slippage
golive_min_opp_per_day  = 5.0    # ≥5 genuine opportunities per day
golive_min_duration_sec = 15.0   # ≥15 seconds spread duration at detection
```

These are REFERENCE values from cross-market arbitrage literature. Shadow mode
may reveal that your specific market mix has different natural parameters.
`optimize.py` will recommend adjusted values based on actual observed data.
The human operator makes the final call — optimize.py recommends, never decides.

### Rule 4: Cluster Quality Gate

Before trading any cluster, verify:
- LLM confidence ≥ CFG.llm_confidence_threshold (default 0.7)
- At least one human spot-check of the pair questions (review cluster_review.log)
- Both markets have volume > CFG.min_market_volume_usd

Low-confidence clusters are logged but never traded, even if they show violations.

### Rule 5: No ML Models Until You Have Data

The ambiguity detection has two layers: structural (rule-based) and semantic
(LLM). Do NOT add an ML classification layer until you have:
- Minimum 200 labeled examples of genuine vs false arb detections
- The labels come from RESOLVED markets (did both resolve consistently?)
- The model is validated on held-out data, not shadow data

Before that, the two-layer system is the right tool.

### Rule 6: Process Hygiene

After every code change:
```bash
# Confirm the new code is actually running (lesson from bear bot 52-hour outage)
ls -la --time-style=full-iso engine/constraints.py   # file mtime
ps -o pid,lstart -p $(pgrep -f shadow.py)            # process must start AFTER
```

If using systemd:
```bash
sudo systemctl restart arb-shadow.service
sudo systemctl status arb-shadow.service
```

A deployed file does nothing until the process is restarted.

### Rule 7: DB Separation

Three separate SQLite databases, never mixed:

| Mode | Command | DB |
|------|---------|-----|
| Shadow | `python shadow.py` | `shadow_run.db` |
| Paper | `python bot.py --portfolio X` | `arb_trades.db` |
| Live | `python bot.py --live ... --db arb_live.db` | `arb_live.db` |

The risk manager (daily loss cap, rolling WR halt) does NOT filter by mode.
Mixing modes in one DB corrupts the kill switch.

---

## Shadow Mode Minimum Criteria

Before generating an optimize.py report, shadow mode must show:

- [ ] Markets discovered: ≥50 active markets
- [ ] Clusters detected: ≥5 clusters with LLM confidence ≥ 0.7
- [ ] At least one violation per type detected (TEMPORAL, THRESHOLD, MUTUAL_EXCLUSIVE)
- [ ] Follow-up polls working: spread_5s, spread_15s populated in shadow_log
- [ ] No feed errors in last 6 hours of logs
- [ ] cluster_review.log reviewed by human for at least 20 pairs

If any criterion is missing, shadow mode is not yet providing reliable data.
Do not run optimize.py until all criteria are met.

---

## Paper Mode Exit Criteria (Before Going Live)

- [ ] ≥30 closed trades logged
- [ ] Rolling 20-trade WR > CFG.rolling_wr_halt_threshold + 5pp
- [ ] Unwind rate (UNWOUND trades / total trades) < 10%
- [ ] Avg realized spread > 1.5¢ (even if below go-live reference of 2¢)
- [ ] No daily loss cap trigger in paper mode
- [ ] At least one trade per violation type (TEMPORAL, THRESHOLD, MUTUAL_EXCLUSIVE)
- [ ] optimize.py has been run and outputs GO LIVE

---

## Live Mode Scale-Up Protocol

Live mode starts at micro scale and scales based on measured data:

| Phase | Size | Condition to advance |
|-------|------|---------------------|
| Micro | CFG.base_position_usdc = $10 | First 30 trades |
| Small | CFG.base_position_usdc = $25 | Unwind rate < 10%, realized spread > 1.5¢ |
| Normal | CFG.base_position_usdc = $50 | 50 trades, WR stable |
| Full | CFG.base_position_usdc = $100 | 100 trades, all metrics healthy |

Never skip phases. Each phase is a measurement experiment.

---

## Performance Targets

| Metric | Target | Halt |
|--------|--------|------|
| Win rate | > 55% | < 45% (20-trade rolling) |
| Unwind rate | < 10% | > 20% (10-trade rolling) |
| Realized spread | > 1.5¢ | < 0.5¢ (20-trade avg) |
| Trades/day | ≥ 3 | < 1 for 3 consecutive days |
| Daily loss cap | — | $25 kill switch |

---

## Empirical Context (Know This)

From academic research on Polymarket arbitrage (2024-2025 data):

- Total arbitrage profit extracted: ~$40M over 12 months
- Breakdown: $10.6M single-market, **$29M multi-condition rebalancing**, small for cross-market
- Single-market arb duration: median 2.7 seconds (dominated by HFT bots)
- Combinatorial arb: 290 episodes over one month in NBA markets (UCLA paper)
- Depth constraint: 76.9% of opportunities limited to ~14.8 shares executable size
- Cross-market failure rate: 62% of LLM-detected dependencies fail to yield profit
  (primarily due to liquidity asymmetry and non-atomic execution)

**What this means for this bot:**
- Mutual exclusive violations are the most proven — prioritize them
- Position size must be small ($10-100) — that's a feature, not a bug (institutions can't compete here)
- 62% failure rate is the baseline WITHOUT good ambiguity detection — good detection is the moat
- Speed is NOT the edge — the edge is semantic analysis quality

---

## Troubleshooting

**No clusters detected after LLM pair detection:**
- Check ANTHROPIC_API_KEY in .env
- Check cluster_review.log for LLM response format errors
- Try reducing llm_pair_batch_size to 25 markets per call
- Verify Anthropic API is reachable: `python -c "import anthropic; print('OK')"`

**All violations rejected at G4 (spread too small):**
- This means slippage is eating the edge — check book depth on flagged pairs
- Consider reducing base_position_usdc to reduce market impact
- May indicate market is too efficient for these market types — pivot to
  mutually exclusive violations only

**High unwind rate (>20%):**
- asyncio.gather() may not be working as expected — verify with logs
- Check if both markets have sufficient depth before submitting
- Consider adding a pre-submission depth check gate

**optimize.py outputs NO-GO repeatedly:**
- Read the specific reason — each has a different fix
- "Too few opportunities" → expand market type coverage or lower volume filter
- "Spread too small" → these markets are too efficient, need different clusters
- "Duration too short" → markets are efficient, consider limit orders instead of market orders

---

## Config Change Log

Format: [DATE] [PARAMETER] [OLD VALUE] → [NEW VALUE] | [REASON] | [DATA SOURCE]

Example:
[2026-07-15] structural_score_threshold 0.70 → 0.65 | Shadow data shows 23% of
  genuine arbs rejected at this gate based on cluster_review.log manual check |
  shadow_report.py output 2026-07-14

---

*Last updated: project initialization*
*Repo: polymarket-logical-arb*
*Reference: bear-oracle-confirmed-sniper CLAUDE.md (2026-06-27)*
*Empirical basis: arXiv:2508.03474 (IMDEA), arXiv:2605.00864 (UCLA)*
