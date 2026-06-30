"""Edge breakdown: raw vs net spread per constraint type, and the fee floor.

    python analysis/edge_breakdown.py [--db shadow_run.db] [--since-hours N]

Answers one question the gate-centric shadow_report.py doesn't isolate: is there
GENUINE risk-free edge, and does it survive costs? For each constraint type it
splits the detected violations into
  - raw spread (pre-cost):       does a real constraint violation exist at all?
  - net spread (slippage + fee): is it actually capturable?
and shows the fee floor, because a thin mutually-exclusive over-round (~1-2c) is
killed by the taker fee alone, before slippage. The ME fee-sensitivity table shows
how many over-rounds would clear the fee at different fee assumptions — the real
Polymarket fee decides whether thin ME is tradeable. Run on any shadow DB to
re-check the clean run after a fee/config change.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CFG  # noqa: E402
from core.database import Database  # noqa: E402


def _stats(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    p = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]  # noqa: E731
    return dict(mn=xs[0], p50=p(0.5), mean=sum(xs) / len(xs), p90=p(0.9), mx=xs[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=CFG.shadow_db_path)
    ap.add_argument("--since-hours", type=float, default=0.0)
    args = ap.parse_args()

    db = Database(args.db)
    where, params = "", ()
    if args.since_hours:
        where = "WHERE detected_at >= ?"
        params = (time.time() - args.since_hours * 3600,)

    # A round buys ~$1 of notional (ME basket ≈ $1; a 2-leg arb's sell+buy ≈ $1),
    # so the taker fee in cents ≈ 100 × fee. Approximate but the right order.
    fee_floor = round(CFG.polymarket_taker_fee * 100, 2)
    noise = CFG.noise_threshold_cents
    g4 = CFG.min_violation_spread_cents

    print("=" * 70)
    print("  EDGE BREAKDOWN —", args.db)
    print(f"  taker fee {CFG.polymarket_taker_fee * 100:.2f}% (~{fee_floor:.2f}c floor/round)"
          f"  |  G4 min {g4}c  |  noise {noise}c")
    print("=" * 70)

    cur = db.conn.execute(
        "SELECT dependency_type, raw_spread_cents, est_spread_cents, reason, "
        f"book_depth_a, book_depth_b FROM shadow_log {where}", params
    )
    rows = db._rows(cur)
    if not rows:
        print("\nNo shadow rows. Run `python shadow.py` first.")
        return

    for dep in ("MUTUAL_EXCLUSIVE", "THRESHOLD", "TEMPORAL"):
        d = [r for r in rows if r["dependency_type"] == dep]
        if not d:
            print(f"\n[{dep}]  (none)")
            continue
        n = len(d)
        raw = [r["raw_spread_cents"] for r in d]
        est = [r["est_spread_cents"] for r in d]
        depth = sorted(min(r["book_depth_a"] or 0, r["book_depth_b"] or 0) for r in d)
        reasons: dict = {}
        for r in d:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        rs, es = _stats(raw), _stats(est)
        raw_real = sum(1 for x in raw if x is not None and x > noise)
        raw_over_fee = sum(1 for x in raw if x is not None and x > fee_floor)
        net_pos = sum(1 for x in est if x is not None and x > 0)
        net_g4 = sum(1 for x in est if x is not None and x >= g4)

        print(f"\n[{dep}]  {n} rows   reasons={reasons}")
        print(f"  raw (pre-cost): min {rs['mn']:+6.2f}  p50 {rs['p50']:+6.2f}  "
              f"mean {rs['mean']:+6.2f}  max {rs['mx']:+6.2f}")
        print(f"  net (slip+fee): min {es['mn']:+6.2f}  p50 {es['p50']:+6.2f}  "
              f"mean {es['mean']:+6.2f}  max {es['mx']:+6.2f}")
        print(f"  raw edge exists (> {noise}c noise) : {raw_real:4}/{n} ({raw_real/n*100:3.0f}%)")
        print(f"  raw clears fee floor ({fee_floor}c)   : {raw_over_fee:4}/{n} ({raw_over_fee/n*100:3.0f}%)")
        print(f"  net survives costs (> 0c)        : {net_pos:4}/{n} ({net_pos/n*100:3.0f}%)")
        print(f"  net would PASS G4 (>= {g4}c)      : {net_g4:4}/{n} ({net_g4/n*100:3.0f}%)")
        med = depth[len(depth) // 2] if depth else 0
        if med <= 0:
            print("  ! median book depth $0 — slippage estimate UNRELIABLE (WS book not loaded)")
        else:
            print(f"  median book depth (thinner leg): ${med:.0f}")

    me_raw = [r["raw_spread_cents"] for r in rows
              if r["dependency_type"] == "MUTUAL_EXCLUSIVE" and r["raw_spread_cents"] is not None]
    if me_raw:
        print("\n[ME FEE SENSITIVITY — raw over-round vs fee only, ignores slippage]")
        print("  fraction of ME over-rounds that clear the fee on raw spread alone:")
        for f in (0.0, 0.005, 0.01, 0.02):
            ok = sum(1 for x in me_raw if x > f * 100)
            print(f"    fee {f*100:4.1f}% (~{f*100:4.1f}c): {ok:3}/{len(me_raw)} clear ({ok/len(me_raw)*100:3.0f}%)")
        print("  -> the REAL Polymarket fee decides whether thin ME is tradeable.")
    print("=" * 70)


if __name__ == "__main__":
    main()
