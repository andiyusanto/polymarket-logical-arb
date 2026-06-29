"""Thread-safe SQLite storage for the logical-arb bot.

One Database class, instantiated against a different file per mode (Rule 7 —
strict DB separation):

    shadow  → shadow_run.db   (shadow_log, cluster_log, window_resolution)
    paper   → arb_trades.db   (trades)
    live    → arb_live.db     (trades)

All tables are created in every file (unused ones stay empty) — same pattern as
the bear bot's single Database class. WAL mode + a process-wide lock keep the
hot WS path from blocking on writes.
"""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from core.models import ArbTrade, ShadowTrade


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self._migrate()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            -- ── Shadow telemetry ─────────────────────────────────────
            CREATE TABLE IF NOT EXISTS shadow_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shadow_uid TEXT UNIQUE,
                detected_at REAL,
                violation_type TEXT,
                market_a_question TEXT,
                market_b_question TEXT,
                cluster_id TEXT,
                dependency_type TEXT,
                raw_spread_cents REAL,
                est_spread_cents REAL,
                structural_score REAL,
                semantic_score REAL,
                confidence_tier TEXT,
                intended_size_usdc REAL,
                spread_5s REAL,
                spread_15s REAL,
                spread_30s REAL,
                spread_60s REAL,
                still_valid_15s INTEGER,
                still_valid_30s INTEGER,
                book_depth_a REAL,
                book_depth_b REAL,
                reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_log(detected_at);
            CREATE INDEX IF NOT EXISTS idx_shadow_type ON shadow_log(violation_type);
            CREATE INDEX IF NOT EXISTS idx_shadow_reason ON shadow_log(reason);

            CREATE TABLE IF NOT EXISTS cluster_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                refreshed_at REAL,
                cluster_id TEXT,
                market_count INTEGER,
                dependency_type TEXT,
                llm_confidence REAL,
                market_questions TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cluster_ts ON cluster_log(refreshed_at);

            CREATE TABLE IF NOT EXISTS window_resolution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_token_id TEXT,
                window_ts REAL,
                detected_spread REAL,
                final_spread REAL,
                resolved_consistently INTEGER
            );

            -- ── Trades (paper + live) ────────────────────────────────
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                mode TEXT,
                detected_at REAL,
                executed_at REAL,
                violation_type TEXT,
                market_a_token TEXT,
                market_b_token TEXT,
                leg_a_side TEXT,
                leg_b_side TEXT,
                size_usdc REAL,
                entry_spread_cents REAL,
                confidence_tier TEXT,
                status TEXT,
                pnl_usdc REAL,
                leg_a_fill_price REAL DEFAULT 0,
                leg_b_fill_price REAL DEFAULT 0,
                notes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_detected ON trades(detected_at);
            CREATE INDEX IF NOT EXISTS idx_trades_type ON trades(violation_type);
        """)

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (SQLite has no
        ADD COLUMN IF NOT EXISTS)."""
        with self._lock:
            tcols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)")}
            for name, decl in (
                ("leg_a_fill_price", "REAL DEFAULT 0"),
                ("leg_b_fill_price", "REAL DEFAULT 0"),
            ):
                if name not in tcols:
                    self.conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")
            self.conn.commit()

    # ── Shadow methods ───────────────────────────────────────────────

    def save_shadow_batch(self, records: list[ShadowTrade]) -> None:
        """Batch upsert ShadowTrade rows keyed by shadow_uid (INSERT OR REPLACE
        so follow-up poll updates overwrite the initial row)."""
        if not records:
            return
        with self._lock:
            self.conn.executemany(
                """INSERT OR REPLACE INTO shadow_log (
                    shadow_uid, detected_at, violation_type,
                    market_a_question, market_b_question, cluster_id,
                    dependency_type, raw_spread_cents, est_spread_cents,
                    structural_score, semantic_score, confidence_tier,
                    intended_size_usdc, spread_5s, spread_15s, spread_30s,
                    spread_60s, still_valid_15s, still_valid_30s,
                    book_depth_a, book_depth_b, reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [self._shadow_row(r) for r in records],
            )
            self.conn.commit()

    @staticmethod
    def _shadow_row(r: ShadowTrade) -> tuple:
        v = r.violation
        p = v.pair
        return (
            getattr(r, "shadow_uid", None) or f"{id(r):x}",
            r.detected_at,
            v.violation_type.value,
            p.market_a.question[:300],
            p.market_b.question[:300],
            p.cluster_id,
            p.dependency_type,
            round(v.raw_spread_cents, 4),
            round(v.estimated_spread_after_slippage_cents, 4),
            round(r.structural_score, 4),
            round(r.semantic_score, 4),
            r.confidence_tier.value,
            round(r.intended_size_usdc, 4),
            r.spread_5s,
            r.spread_15s,
            r.spread_30s,
            r.spread_60s,
            None if r.still_valid_at_15s is None else int(r.still_valid_at_15s),
            None if r.still_valid_at_30s is None else int(r.still_valid_at_30s),
            round(p.market_a.depth_usd, 2),
            round(p.market_b.depth_usd, 2),
            r.reason,
        )

    def save_cluster(
        self,
        cluster_id: str,
        market_count: int,
        dependency_type: str,
        llm_confidence: float,
        market_questions_json: str,
        refreshed_at: float = None,
    ) -> None:
        # All clusters from one refresh must share a timestamp so the
        # "latest refresh" queries (MAX(refreshed_at)) return the whole batch,
        # not just the last row written.
        with self._lock:
            self.conn.execute(
                "INSERT INTO cluster_log (refreshed_at, cluster_id, market_count, "
                "dependency_type, llm_confidence, market_questions) "
                "VALUES (?,?,?,?,?,?)",
                (
                    refreshed_at if refreshed_at is not None else time.time(),
                    cluster_id,
                    market_count,
                    dependency_type,
                    llm_confidence,
                    market_questions_json,
                ),
            )
            self.conn.commit()

    def save_resolution(
        self,
        market_token_id: str,
        window_ts: float,
        detected_spread: float,
        final_spread: float,
        resolved_consistently: bool,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO window_resolution (market_token_id, window_ts, "
                "detected_spread, final_spread, resolved_consistently) "
                "VALUES (?,?,?,?,?)",
                (
                    market_token_id,
                    window_ts,
                    detected_spread,
                    final_spread,
                    int(resolved_consistently),
                ),
            )
            self.conn.commit()

    def shadow_total(self, since_ts: float = 0.0) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM shadow_log WHERE detected_at >= ?", (since_ts,)
        )
        return cur.fetchone()[0]

    def shadow_pass_count(self, since_ts: float = 0.0) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM shadow_log WHERE detected_at >= ? AND reason='PASS'",
            (since_ts,),
        )
        return cur.fetchone()[0]

    def shadow_passes(self, since_ts: float = 0.0) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM shadow_log WHERE detected_at >= ? AND reason='PASS' "
            "ORDER BY detected_at DESC",
            (since_ts,),
        )
        return self._rows(cur)

    def shadow_all(self, since_ts: float = 0.0) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM shadow_log WHERE detected_at >= ? ORDER BY detected_at",
            (since_ts,),
        )
        return self._rows(cur)

    def shadow_reason_counts(self, since_ts: float = 0.0) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT reason, COUNT(*) FROM shadow_log WHERE detected_at >= ? "
            "GROUP BY reason",
            (since_ts,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    def shadow_span_days(self) -> float:
        cur = self.conn.execute(
            "SELECT MIN(detected_at), MAX(detected_at) FROM shadow_log"
        )
        lo, hi = cur.fetchone()
        if not lo or not hi or hi <= lo:
            return 0.0
        return (hi - lo) / 86400.0

    # ── Trade methods ────────────────────────────────────────────────

    def save_trade(self, t: ArbTrade) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t.id,
                    t.mode,
                    t.detected_at,
                    t.executed_at,
                    t.violation_type,
                    t.market_a_token,
                    t.market_b_token,
                    t.leg_a_side,
                    t.leg_b_side,
                    t.size_usdc,
                    t.entry_spread_cents,
                    t.confidence_tier,
                    t.status,
                    t.pnl_usdc,
                    t.leg_a_fill_price,
                    t.leg_b_fill_price,
                    t.notes,
                ),
            )
            self.conn.commit()

    def close_trade(self, tid: str, pnl: float, status: str = "CLOSED") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE trades SET pnl_usdc=?, status=? WHERE id=?",
                (round(pnl, 6), status, tid),
            )
            self.conn.commit()

    def active_arb_count(self) -> int:
        """Open or partially-filled positions — for the max_concurrent gate."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','PARTIAL')"
        )
        return cur.fetchone()[0]

    def recent(self, n: int = 15) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades ORDER BY detected_at DESC LIMIT ?", (n,)
        )
        return self._rows(cur)

    def all_trades(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM trades ORDER BY detected_at")
        return self._rows(cur)

    def daily_pnl(self) -> float:
        ts = self._utc_midnight()
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc), 0) FROM trades WHERE detected_at >= ?",
            (ts,),
        )
        return cur.fetchone()[0]

    def daily_count(self) -> int:
        ts = self._utc_midnight()
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE detected_at >= ?", (ts,)
        )
        return cur.fetchone()[0]

    def rolling_wr(self, n: int = 20) -> Optional[float]:
        """Win rate over the last n closed trades. None if fewer than n."""
        cur = self.conn.execute(
            "SELECT pnl_usdc FROM trades WHERE status IN ('CLOSED','UNWOUND') "
            "ORDER BY executed_at DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        if len(rows) < n:
            return None
        wins = sum(1 for (pnl,) in rows if pnl > 0)
        return wins / n

    def unwind_rate(self, n: int = 20) -> Optional[float]:
        """Fraction of the last n closed trades that were UNWOUND."""
        cur = self.conn.execute(
            "SELECT status FROM trades WHERE status IN ('CLOSED','UNWOUND','PARTIAL') "
            "ORDER BY executed_at DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        unwound = sum(1 for (s,) in rows if s == "UNWOUND")
        return unwound / len(rows)

    def lifetime_stats(self) -> dict:
        cur = self.conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END), 0),
                      COALESCE(SUM(pnl_usdc), 0),
                      COALESCE(SUM(CASE WHEN status='UNWOUND' THEN 1 ELSE 0 END), 0)
               FROM trades WHERE status IN ('CLOSED','UNWOUND')"""
        )
        total, wins, pnl, unwound = cur.fetchone()
        return {
            "total": total,
            "wins": wins,
            "pnl": round(pnl, 4),
            "wr": round(wins / total * 100, 1) if total else 0.0,
            "unwound": unwound,
            "unwind_rate": round(unwound / total * 100, 1) if total else 0.0,
            "expectancy": round(pnl / total, 4) if total else 0.0,
        }

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _utc_midnight() -> float:
        return (
            datetime.now(tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

    def _rows(self, cur: sqlite3.Cursor) -> list[dict]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
