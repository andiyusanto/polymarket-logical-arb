"""Shadow data → recommended config + GO/NO-GO decision.

    python analysis/optimize.py --min-days 14

This is the single source of truth for the go-live decision (CLAUDE.md Rule 3).
It NEVER writes config — it only recommends. A human reads the output and edits
core/config.py manually, logging the change in CLAUDE.md's Config Change Log.

Layer 1 (hard gates) decides GO/NO-GO against the golive_* reference thresholds.
Layer 2 recommends tuned values from the observed PASS distribution.
"""

import argparse
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CFG  # noqa: E402
from core.database import Database  # noqa: E402


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else 0.0


def _spread_duration(p: dict) -> float:
    """Estimate seconds the PASS spread stayed >= min, from follow-up polls."""
    m = CFG.min_violation_spread_cents
    if p.get("spread_60s") is not None and p["spread_60s"] >= m:
        return 60.0
    if p.get("still_valid_30s"):
        return 30.0
    if p.get("still_valid_15s"):
        return 15.0
    if p.get("spread_5s") is not None and p["spread_5s"] >= m:
        return 5.0
    return 2.0  # was valid at detection, gone by the first poll


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=CFG.shadow_db_path)
    ap.add_argument("--min-days", type=float, default=CFG.golive_min_shadow_days)
    args = ap.parse_args()

    db = Database(args.db)
    span = db.shadow_span_days()
    passes = db.shadow_passes()
    n = len(passes)

    print("=" * 64)
    print("  SHADOW MODE OPTIMIZATION REPORT")
    print(f"  Observation period: {span:.2f} days (min required: {args.min_days})")
    print("=" * 64)

    if n == 0:
        print("\nNo PASS signals recorded — cannot evaluate. Keep shadowing.")
        print("\n[GO/NO-GO DECISION]\n  → NO-GO (no PASS signals)")
        return

    spreads = [p["est_spread_cents"] for p in passes]
    avg_spread = _mean(spreads)
    opp_per_day = n / span if span > 0 else 0.0
    durations = [_spread_duration(p) for p in passes]
    avg_duration = _mean(durations)

    # ── Layer 1 — hard gates ─────────────────────────────────────────
    g_spread = avg_spread >= CFG.golive_min_spread_cents
    g_opp = opp_per_day >= CFG.golive_min_opp_per_day
    g_dur = avg_duration >= CFG.golive_min_duration_sec
    g_days = span >= args.min_days

    def mark(ok):
        return "✓" if ok else "✗"

    print("\n[LAYER 1 — HARD GATES STATUS]")
    print(f"  {mark(g_spread)} Avg spread after slippage: {avg_spread:.2f}¢  "
          f"(reference: >={CFG.golive_min_spread_cents}¢)")
    print(f"  {mark(g_opp)} Genuine opp per day: {opp_per_day:.2f}        "
          f"(reference: >={CFG.golive_min_opp_per_day})")
    print(f"  {mark(g_dur)} Avg spread duration: {avg_duration:.1f}s        "
          f"(reference: >={CFG.golive_min_duration_sec}s)")
    print(f"  {mark(g_days)} Observation period: {span:.1f}d          "
          f"(reference: >={args.min_days}d)")

    # ── Decision ─────────────────────────────────────────────────────
    go = g_spread and g_opp and g_dur and g_days
    print("\n[GO/NO-GO DECISION]")
    if go:
        print("  → GO LIVE")
    else:
        reasons = []
        if not g_days:
            reasons.append(f"only {span:.1f} of {args.min_days} days observed")
        if not g_spread:
            reasons.append(f"avg spread {avg_spread:.2f}¢ < {CFG.golive_min_spread_cents}¢")
        if not g_opp:
            reasons.append(f"only {opp_per_day:.1f} opp/day < {CFG.golive_min_opp_per_day}")
        if not g_dur:
            reasons.append(f"avg duration {avg_duration:.1f}s < {CFG.golive_min_duration_sec}s")
        print(f"  → NO-GO ({'; '.join(reasons)})")

    # ── Layer 2 — recommended config ─────────────────────────────────
    print("\n[LAYER 2 — OPTIMAL CONFIG (recommendations only — human applies)]")

    # structural / semantic floors: don't reject the genuine PASSes we observed.
    sem_scores = [p["semantic_score"] for p in passes if p["confidence_tier"] != "HIGH"
                  or p["semantic_score"] > 0]
    struct_scores = [p["structural_score"] for p in passes if p["structural_score"] > 0]
    if struct_scores:
        rec_struct = max(0.5, round(min(struct_scores) - 0.05, 2))
        print(f"  CFG.structural_score_threshold = {rec_struct}  "
              f"(min observed PASS struct = {min(struct_scores):.2f})")
    if sem_scores:
        rec_sem = max(0.70, round(min(sem_scores) - 0.02, 2))
        print(f"  CFG.semantic_score_threshold   = {rec_sem}  "
              f"(min observed PASS sem = {min(sem_scores):.2f})")

    # position size: keep impact small vs observed book depth on PASS legs.
    depths = []
    for p in passes:
        da, db_ = p.get("book_depth_a") or 0, p.get("book_depth_b") or 0
        if da and db_:
            depths.append(min(da, db_))
    if depths:
        rec_size = max(10.0, round(min(statistics.median(depths) * 0.25,
                                       CFG.base_position_usdc), 0))
        print(f"  CFG.base_position_usdc         = ${rec_size:.0f}  "
              f"(median min-leg depth ${statistics.median(depths):.0f}; "
              f"sized to ~25% to limit impact)")

    # blackout hours: hours where PASS spreads decay before 15s most often.
    by_hour_total: dict = defaultdict(int)
    by_hour_decayed: dict = defaultdict(int)
    for p in passes:
        hr = time.gmtime(p["detected_at"]).tm_hour
        by_hour_total[hr] += 1
        if p.get("still_valid_15s") == 0:
            by_hour_decayed[hr] += 1
    blackout = sorted(
        h for h, tot in by_hour_total.items()
        if tot >= 3 and by_hour_decayed[h] / tot > 0.5
    )
    print(f"  CFG.blackout_hours             = {set(blackout) or '{}'}  "
          f"(UTC hours where >50% of PASS spreads decayed before 15s)")

    # best violation type to prioritize: highest mean spread * volume.
    by_type: dict = defaultdict(list)
    for p in passes:
        by_type[p["violation_type"]].append(p["est_spread_cents"])
    if by_type:
        best = max(by_type.items(), key=lambda kv: _mean(kv[1]) * len(kv[1]))
        print(f"  Prioritize violation type: {best[0].upper()} "
              f"(mean {_mean(best[1]):.2f}¢, n={len(best[1])})")

    # ── Confidence ───────────────────────────────────────────────────
    conf = "LOW" if n < 20 else "MEDIUM" if n < 50 else "HIGH"
    scale = 25 if n < 20 else 50 if n < 50 else 100
    print("\n[CONFIDENCE]")
    print(f"  Shadow sample size: {n} PASS signals over {span:.1f} days")
    print(f"  Statistical confidence: {conf}")
    print(f"  Recommendation: scale live size to {scale}% until N>=50")
    print("=" * 64)


if __name__ == "__main__":
    main()
