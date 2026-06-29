"""Shadow-mode gate breakdown + PASS-signal analysis.

    python analysis/shadow_report.py [--db shadow_run.db] [--since-hours N]

Reports cluster quality, violation frequency, the spread distribution (raw vs
after-slippage), the spread-decay curve from the follow-up polls, the ambiguity
filter rejection rates, the confidence-tier mix, and the PASS signals that would
have traded live. Mirrors the bear bot's shadow_report.py telemetry pattern.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CFG  # noqa: E402
from core.database import Database  # noqa: E402
from core.shadow import REASON_LABELS  # noqa: E402


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else 0.0


def _pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return 0.0
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=CFG.shadow_db_path)
    ap.add_argument("--since-hours", type=float, default=0.0)
    args = ap.parse_args()

    db = Database(args.db)
    since = time.time() - args.since_hours * 3600 if args.since_hours else 0.0
    rows = db.shadow_all(since)
    total = len(rows)

    print("=" * 64)
    print("  SHADOW MODE REPORT —", args.db)
    span = db.shadow_span_days()
    print(f"  Observation span: {span:.2f} days   Evaluations: {total}")
    print("=" * 64)

    if not total:
        print("\nNo shadow records yet. Run `python shadow.py` first.")
        return

    # ── Cluster quality ──────────────────────────────────────────────
    cur = db.conn.execute(
        "SELECT cluster_id, dependency_type, llm_confidence, market_count "
        "FROM cluster_log WHERE refreshed_at = (SELECT MAX(refreshed_at) FROM cluster_log)"
    )
    clusters = db._rows(cur)
    print("\n[CLUSTER QUALITY]")
    if clusters:
        confs = [c["llm_confidence"] for c in clusters]
        flagged = sum(1 for c in confs if c < CFG.llm_confidence_threshold)
        by_type: dict = {}
        for c in clusters:
            by_type[c["dependency_type"]] = by_type.get(c["dependency_type"], 0) + 1
        print(f"  Clusters (latest refresh): {len(clusters)}  {by_type}")
        print(f"  Avg LLM confidence: {_mean(confs):.2f}")
        print(f"  Below confidence gate ({CFG.llm_confidence_threshold}): {flagged}")
    else:
        print("  (no cluster_log rows)")

    # ── Violation frequency ──────────────────────────────────────────
    print("\n[VIOLATION FREQUENCY BY TYPE]")
    vt: dict = {}
    for r in rows:
        vt[r["violation_type"]] = vt.get(r["violation_type"], 0) + 1
    for k, v in sorted(vt.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} {v:6d}  ({v / total * 100:.1f}%)")

    # ── Gate / reason breakdown ──────────────────────────────────────
    print("\n[GATE BREAKDOWN]")
    counts = db.shadow_reason_counts(since)
    for reason, label in REASON_LABELS.items():
        c = counts.get(reason, 0)
        print(f"  {label:34s} {c:6d}  ({c / total * 100:.1f}%)")

    # ── Spread analysis ──────────────────────────────────────────────
    raw = [r["raw_spread_cents"] for r in rows]
    est = [r["est_spread_cents"] for r in rows]
    print("\n[SPREAD ANALYSIS — all detected violations, cents]")
    print(f"  raw       mean {_mean(raw):6.2f}  p50 {_pct(raw,50):6.2f}  p90 {_pct(raw,90):6.2f}")
    print(f"  slippage  mean {_mean(est):6.2f}  p50 {_pct(est,50):6.2f}  p90 {_pct(est,90):6.2f}")
    eaten = [a - b for a, b in zip(raw, est)]
    print(f"  eaten by slippage: mean {_mean(eaten):.2f}¢")

    # ── Spread decay (follow-up polls) ───────────────────────────────
    passes = db.shadow_passes(since)
    print("\n[SPREAD DECAY — PASS signals, cents]")
    if passes:
        for label, key in (("t+0", "est_spread_cents"), ("t+5s", "spread_5s"),
                           ("t+15s", "spread_15s"), ("t+30s", "spread_30s"),
                           ("t+60s", "spread_60s")):
            vals = [p[key] for p in passes if p.get(key) is not None]
            print(f"  {label:6s} mean {_mean(vals):6.2f}  (n={len(vals)})")
        v15 = [p["still_valid_15s"] for p in passes if p.get("still_valid_15s") is not None]
        v30 = [p["still_valid_30s"] for p in passes if p.get("still_valid_30s") is not None]
        if v15:
            print(f"  still valid @15s: {sum(v15)}/{len(v15)} ({sum(v15)/len(v15)*100:.0f}%)")
        if v30:
            print(f"  still valid @30s: {sum(v30)}/{len(v30)} ({sum(v30)/len(v30)*100:.0f}%)")
    else:
        print("  (no PASS signals yet)")

    # ── Ambiguity filter ─────────────────────────────────────────────
    print("\n[AMBIGUITY FILTER]")
    g2 = counts.get("G2_STRUCTURAL", 0)
    g3 = counts.get("G3_SEMANTIC", 0)
    nonme = sum(v for k, v in vt.items() if k != "mutual_exclusive")
    if nonme:
        print(f"  structural reject rate: {g2 / nonme * 100:.1f}% of non-ME violations")
        print(f"  semantic reject rate:   {g3 / nonme * 100:.1f}% of non-ME violations")
    else:
        print("  (no temporal/threshold violations to filter)")

    # ── Confidence tiers ─────────────────────────────────────────────
    print("\n[CONFIDENCE TIER DISTRIBUTION]")
    tiers: dict = {}
    for r in rows:
        tiers[r["confidence_tier"]] = tiers.get(r["confidence_tier"], 0) + 1
    for k, v in sorted(tiers.items()):
        print(f"  {k:8s} {v:6d}  ({v / total * 100:.1f}%)")

    # ── PASS signals ─────────────────────────────────────────────────
    print(f"\n[PASS SIGNALS — would have traded live: {len(passes)}]")
    for p in passes[:10]:
        print(f"  {p['violation_type']:16s} {p['est_spread_cents']:5.2f}¢ "
              f"[{p['confidence_tier']}] {p['market_a_question'][:50]}")
    print("=" * 64)


if __name__ == "__main__":
    main()
