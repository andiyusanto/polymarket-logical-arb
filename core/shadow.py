"""Bilateral-trade shadow telemetry logger.

Ported from the bear bot's ShadowLogger (in-memory counters + batched DB flush so
the hot WS path never touches SQLite), extended for the arb bot:

  - Records are keyed by shadow_uid. Follow-up polls re-emit the same uid with
    updated spread_5s/15s/... fields; the buffer keeps the latest version and the
    DB write is INSERT OR REPLACE, so a single row evolves as polls land.
  - Counters track the gate/reason breakdown by UNIQUE uid (a re-emit from a
    follow-up poll does not double-count).

Flush cadence is driven by the entrypoint (every CFG.shadow_flush_interval_sec).
"""

import logging
import time

from core.models import ShadowTrade

log = logging.getLogger("arb.shadow")

# Canonical reasons, in display order (PASS last).
REASONS = [
    "G1_NO_CLUSTER",
    "G2_STRUCTURAL",
    "G3_SEMANTIC",
    "G4_SPREAD_TOO_SMALL",
    "G5_ILLIQUID",
    "G6_BLACKOUT",
    "G7_MAX_CONCURRENT",
    "PASS",
]

REASON_LABELS = {
    "G1_NO_CLUSTER": "G1 — No confirmed cluster",
    "G2_STRUCTURAL": "G2 — Structural wording trap",
    "G3_SEMANTIC": "G3 — Semantic ambiguity",
    "G4_SPREAD_TOO_SMALL": "G4 — Spread eaten by slippage",
    "G5_ILLIQUID": "G5 — Book too illiquid",
    "G6_BLACKOUT": "G6 — Blackout hour",
    "G7_MAX_CONCURRENT": "G7 — Concurrency limit",
    "PASS": "PASS — would have traded",
}


class ShadowLogger:
    FLUSH_BATCH = 100

    def __init__(self) -> None:
        self._counts: dict[str, int] = {r: 0 for r in REASONS}
        self._buffer: dict[str, ShadowTrade] = {}   # uid -> latest record
        self._seen_uids: set[str] = set()
        self._total = 0
        self._session_start = time.time()

    # ── Hot-path API ─────────────────────────────────────────────────
    def record(self, st: ShadowTrade) -> None:
        uid = getattr(st, "shadow_uid", None)
        if uid is None:
            uid = f"{id(st):x}"
            st.shadow_uid = uid
        if uid not in self._seen_uids:
            self._seen_uids.add(uid)
            self._total += 1
            self._counts[st.reason] = self._counts.get(st.reason, 0) + 1
        self._buffer[uid] = st   # latest version wins (follow-up updates)

    # ── Batch flush ──────────────────────────────────────────────────
    def flush(self, db) -> int:
        if not self._buffer:
            return 0
        records = list(self._buffer.values())
        self._buffer.clear()
        db.save_shadow_batch(records)
        return len(records)

    def flush_all(self, db) -> int:
        return self.flush(db)

    # ── Dashboard helpers ────────────────────────────────────────────
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def total(self) -> int:
        return self._total

    def passes(self) -> int:
        return self._counts.get("PASS", 0)

    def session_elapsed(self) -> float:
        return time.time() - self._session_start

    def rate_per_min(self) -> float:
        elapsed = self.session_elapsed()
        return self._total / (elapsed / 60) if elapsed > 0 else 0.0
