"""Paper / live trade analysis.

    python analysis/analyze.py --paper     # arb_trades.db
    python analysis/analyze.py --live      # arb_live.db
    python analysis/analyze.py --watch     # auto-refresh every 5s

Metrics: win rate, avg spread captured, avg per-leg fill, partial-fill rate,
unwind rate, and PnL broken down by violation type.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CFG  # noqa: E402
from core.database import Database  # noqa: E402


def _report(db: Database) -> None:
    rows = db.all_trades()
    closed = [r for r in rows if r["status"] in ("CLOSED", "UNWOUND")]
    stats = db.lifetime_stats()

    print("=" * 60)
    print(f"  TRADE ANALYSIS — {db.path}")
    print("=" * 60)
    print(f"  Total trades logged : {len(rows)}")
    print(f"  Closed/unwound      : {len(closed)}")
    print(f"  Win rate            : {stats['wr']:.1f}%  (target >55%, halt <45%)")
    print(f"  Total PnL           : ${stats['pnl']:+.4f}")
    print(f"  Expectancy/trade    : ${stats['expectancy']:+.4f}")

    partial = sum(1 for r in rows if r["status"] == "PARTIAL")
    cancelled = sum(1 for r in rows if r["status"] == "CANCELLED")
    print(f"  Unwound             : {stats['unwound']}  "
          f"({stats['unwind_rate']:.1f}%  — target <10%, halt >20%)")
    print(f"  Partial (open)      : {partial}")
    print(f"  Cancelled (no fill) : {cancelled}")

    if closed:
        spreads = [r["entry_spread_cents"] for r in closed]
        print(f"  Avg entry spread    : {sum(spreads) / len(spreads):.2f}¢  "
              f"(target >1.5¢)")

    # ── By violation type ────────────────────────────────────────────
    print("\n  PnL BY VIOLATION TYPE")
    by_type: dict = {}
    for r in closed:
        d = by_type.setdefault(r["violation_type"], {"n": 0, "wins": 0, "pnl": 0.0,
                                                      "spread": 0.0})
        d["n"] += 1
        d["wins"] += 1 if r["pnl_usdc"] > 0 else 0
        d["pnl"] += r["pnl_usdc"]
        d["spread"] += r["entry_spread_cents"]
    for vt, d in sorted(by_type.items(), key=lambda x: -x[1]["pnl"]):
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        print(f"    {vt:18s} n={d['n']:3d} wr={wr:5.1f}% "
              f"pnl=${d['pnl']:+8.4f} avg_spread={d['spread'] / d['n']:.2f}¢"
              if d["n"] else f"    {vt}: no closed trades")
    print("=" * 60)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--db", default="")
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()

    path = args.db or (CFG.live_db_path if args.live else CFG.db_path)
    db = Database(path)

    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")  # clear screen
                _report(db)
                time.sleep(5)
        except KeyboardInterrupt:
            pass
    else:
        _report(db)


if __name__ == "__main__":
    main()
