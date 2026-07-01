"""Identify the suspicious signal groups in a shadow DB.

    python3 analysis/inspect_signals.py [--db shadow_run.db]

(1) Temporal cluster identity — is the 44.6c temporal block one mis-paired
    cluster or many? Groups temporal violations by cluster + leg-pair.
(2) Threshold PASS leg pairing — shows what each threshold PASS was legged
    against, to eyeball whether it is a genuine same-metric ladder.
"""
import argparse
import sqlite3
import textwrap


def _wrap(s: str, w: int = 60) -> str:
    return textwrap.shorten(s or "", width=w, placeholder=" …")


def temporal_clusters(conn: sqlite3.Connection) -> None:
    rows = conn.execute("""
        SELECT cluster_id, market_a_question, market_b_question,
               COUNT(*)                        AS n,
               ROUND(AVG(raw_spread_cents), 2) AS raw_c,
               ROUND(AVG(est_spread_cents), 2) AS net_c,
               ROUND(MIN(raw_spread_cents), 2) AS raw_min,
               ROUND(MAX(raw_spread_cents), 2) AS raw_max,
               GROUP_CONCAT(DISTINCT reason)   AS reasons
        FROM shadow_log
        WHERE violation_type = 'temporal'
        GROUP BY cluster_id, market_a_question, market_b_question
        ORDER BY n DESC
    """).fetchall()

    print("=" * 78)
    print(f"  TEMPORAL — {len(rows)} distinct cluster/leg-pairs")
    print("=" * 78)
    for r in rows:
        cid, qa, qb, n, raw, net, rmin, rmax, reasons = r
        print(f"\n  n={n:<5} raw≈{raw:<6} net≈{net:<6} range[{rmin}..{rmax}]  {reasons}")
        print(f"    cluster: {cid}")
        print(f"    A: {_wrap(qa)}")
        print(f"    B: {_wrap(qb)}")


def threshold_passes(conn: sqlite3.Connection) -> None:
    rows = conn.execute("""
        SELECT ROUND(raw_spread_cents, 2), ROUND(est_spread_cents, 2),
               confidence_tier, ROUND(book_depth_a, 0), ROUND(book_depth_b, 0),
               still_valid_15s, still_valid_30s,
               market_a_question, market_b_question
        FROM shadow_log
        WHERE violation_type = 'threshold' AND reason = 'PASS'
        ORDER BY detected_at
    """).fetchall()

    print("\n" + "=" * 78)
    print(f"  THRESHOLD PASS — {len(rows)} signals")
    print("=" * 78)
    for raw, net, tier, da, db, v15, v30, qa, qb in rows:
        print(f"\n  raw={raw}c net={net}c [{tier}]  depth_a=${da} depth_b=${db}"
              f"  valid@15s={v15} @30s={v30}")
        print(f"    A: {_wrap(qa)}")
        print(f"    B: {_wrap(qb)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="shadow_run.db")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    try:
        temporal_clusters(conn)
        threshold_passes(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
