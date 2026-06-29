"""Rich terminal dashboard for the logical-arb bot.

Ported from the bear bot's dashboard, with arb panels:
  - CLUSTERS         (count + quality, replaces the bear bot's REGIME panel)
  - VIOLATION TYPES  (frequency breakdown, replaces the GATE breakdown)
  - GATE BREAKDOWN   (where signals die)
  - TOP OPPORTUNITIES(best recent PASS signals by spread)

Standalone live monitor — reads a DB and refreshes:

    python ui/dashboard.py            # shadow_run.db
    python ui/dashboard.py --trades   # arb_trades.db
    python ui/dashboard.py --db arb_live.db
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CFG  # noqa: E402
from core.database import Database  # noqa: E402
from core.shadow import REASON_LABELS  # noqa: E402

from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402


def _clusters_panel(db: Database) -> Panel:
    cur = db.conn.execute(
        "SELECT dependency_type, COUNT(*), AVG(llm_confidence) FROM cluster_log "
        "WHERE refreshed_at=(SELECT MAX(refreshed_at) FROM cluster_log) "
        "GROUP BY dependency_type"
    )
    t = Table(expand=True)
    t.add_column("Dependency"); t.add_column("Clusters", justify="right")
    t.add_column("Avg conf", justify="right")
    total = 0
    for dep, n, conf in cur.fetchall():
        total += n
        t.add_row(dep, str(n), f"{(conf or 0):.2f}")
    return Panel(t, title=f"CLUSTERS ({total})", border_style="cyan")


def _violation_panel(db: Database) -> Panel:
    cur = db.conn.execute(
        "SELECT violation_type, COUNT(*) FROM shadow_log GROUP BY violation_type "
        "ORDER BY 2 DESC"
    )
    t = Table(expand=True)
    t.add_column("Violation type"); t.add_column("Count", justify="right")
    for vt, n in cur.fetchall():
        t.add_row(vt, str(n))
    return Panel(t, title="VIOLATION TYPES", border_style="magenta")


def _gate_panel(db: Database) -> Panel:
    counts = db.shadow_reason_counts()
    total = sum(counts.values()) or 1
    t = Table(expand=True)
    t.add_column("Gate"); t.add_column("Count", justify="right")
    t.add_column("%", justify="right")
    for reason, label in REASON_LABELS.items():
        c = counts.get(reason, 0)
        style = "green" if reason == "PASS" else None
        t.add_row(label, str(c), f"{c / total * 100:.1f}", style=style)
    return Panel(t, title="GATE BREAKDOWN", border_style="yellow")


def _opportunities_panel(db: Database) -> Panel:
    passes = db.shadow_passes()
    passes.sort(key=lambda p: -(p.get("est_spread_cents") or 0))
    t = Table(expand=True)
    t.add_column("Type"); t.add_column("Spread", justify="right")
    t.add_column("Tier"); t.add_column("Market")
    for p in passes[:10]:
        t.add_row(p["violation_type"], f"{p['est_spread_cents']:.2f}¢",
                  p["confidence_tier"], (p["market_a_question"] or "")[:46])
    return Panel(t, title="TOP OPPORTUNITIES", border_style="green")


def _header(db: Database) -> Panel:
    total = db.shadow_total()
    passes = db.shadow_pass_count()
    span = db.shadow_span_days()
    txt = (f"[bold]Polymarket Logical-Arb[/bold]   DB: {db.path}\n"
           f"Span: {span:.2f}d   Evaluations: {total}   "
           f"PASS: [green]{passes}[/green]   "
           f"min-spread: {CFG.min_violation_spread_cents}¢   "
           f"{time.strftime('%H:%M:%S')}")
    return Panel(txt, border_style="white")


def render(db: Database) -> Group:
    return Group(
        _header(db),
        _clusters_panel(db),
        _violation_panel(db),
        _gate_panel(db),
        _opportunities_panel(db),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", action="store_true", help="use arb_trades.db")
    ap.add_argument("--db", default="")
    ap.add_argument("--once", action="store_true", help="render once and exit")
    args = ap.parse_args()
    path = args.db or (CFG.db_path if args.trades else CFG.shadow_db_path)
    db = Database(path)

    if args.once:
        Console().print(render(db))
        return
    try:
        with Live(render(db), refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(2)
                live.update(render(db))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
