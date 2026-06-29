"""Shadow mode entrypoint — zero trades, full constraint + gate telemetry.

Boot sequence:
  1. discover all active markets (Gamma, volume-filtered)
  2. build the cluster map (NegRisk grouping + LLM pair detection)
  3. subscribe the WebSocket book cache to every clustered token
  4. on each book update, evaluate the token's cluster → ShadowTrade telemetry
  5. follow-up polls (5/15/30/60s) measure spread decay
  6. periodic: flush telemetry, log cluster-quality stats, refresh clusters

Usage:
  python shadow.py                 # run until Ctrl-C
  python shadow.py --duration 1800 # run 30 minutes then exit cleanly
"""

import argparse
import asyncio
import contextlib
import logging
import time

from core.config import CFG
from core.database import Database
from core.logging_setup import setup_logging
from core.shadow import REASON_LABELS, ShadowLogger
from engine.signal import ArbEngine
from feeds.clustering import MarketClusterer
from feeds.markets import LocalOrderBookCache, MarketDiscovery

log = logging.getLogger("arb.shadow.main")


class ShadowApp:
    def __init__(self) -> None:
        self.db = Database(CFG.shadow_db_path)
        self.logger = ShadowLogger()
        self.discovery = MarketDiscovery()
        self.clusterer = MarketClusterer()
        self.cluster_map = None
        self._eval_queue: asyncio.Queue = asyncio.Queue()
        self._pending: set[str] = set()
        self.cache = LocalOrderBookCache(on_book_update=self._on_book_update)
        self.engine = ArbEngine(
            self.cache, cluster_map=None, shadow_logger=self.logger, db=self.db
        )
        self._running = True

    # ── WS callback (sync, runs in loop thread) ──────────────────────
    def _on_book_update(self, token_id: str) -> None:
        if self.cluster_map is None:
            return
        cid = self.cluster_map._token_to_cluster.get(token_id)
        if cid is None or cid in self._pending:
            return
        self._pending.add(cid)
        self._eval_queue.put_nowait(cid)

    # ── Boot ─────────────────────────────────────────────────────────
    async def _build_clusters(self) -> None:
        markets = await self.discovery.get_all_active_markets()
        self.cluster_map = await self.clusterer.build_cluster_map(markets)
        self.engine.cluster_map = self.cluster_map
        self.cache.set_token_ids(self.cluster_map.all_token_ids())
        self._log_cluster_quality()
        import json as _json

        refresh_ts = time.time()   # one timestamp for the whole refresh batch
        for cid in self.cluster_map:
            meta = self.cluster_map.meta.get(cid, {})
            qs = [m.question for m in self.cluster_map[cid]]
            self.db.save_cluster(
                cid,
                len(qs),
                meta.get("dependency_type", "?"),
                meta.get("llm_confidence", 0.0),
                _json.dumps(qs[:12]),
                refreshed_at=refresh_ts,
            )

    def _log_cluster_quality(self) -> None:
        if not self.cluster_map:
            return
        by_type: dict[str, int] = {}
        confs = []
        for cid in self.cluster_map:
            meta = self.cluster_map.meta.get(cid, {})
            t = meta.get("dependency_type", "?")
            by_type[t] = by_type.get(t, 0) + 1
            confs.append(meta.get("llm_confidence", 0.0))
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        log.info(
            "CLUSTERS: %d total %s | avg conf %.2f | %d LLM pairs",
            len(self.cluster_map),
            by_type,
            avg_conf,
            len(self.cluster_map.pairs),
        )

    # ── Worker loops ─────────────────────────────────────────────────
    async def _consumer(self) -> None:
        while self._running:
            try:
                cid = await asyncio.wait_for(self._eval_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._pending.discard(cid)
            cluster = (
                self.cluster_map.get_cluster_for(self.cluster_map[cid][0].token_id)
                if cid in self.cluster_map
                else None
            )
            if cluster is not None:
                with contextlib.suppress(Exception):
                    await self.engine.evaluate_cluster(cluster)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(CFG.shadow_flush_interval_sec)
            n = self.logger.flush(self.db)
            if n:
                log.debug("flushed %d shadow records", n)

    async def _status_loop(self) -> None:
        while self._running:
            await asyncio.sleep(900)  # every 15 min
            self._log_cluster_quality()
            counts = self.logger.counts()
            top = sorted(
                ((r, c) for r, c in counts.items() if r != "PASS" and c),
                key=lambda x: -x[1],
            )[:3]
            log.info(
                "SHADOW: %d evals (%.1f/min) | PASS=%d | top rejects: %s",
                self.logger.total(),
                self.logger.rate_per_min(),
                self.logger.passes(),
                ", ".join(f"{REASON_LABELS.get(r, r)}={c}" for r, c in top) or "none",
            )

    async def _refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(CFG.cluster_refresh_interval_sec)
            log.info("Refreshing cluster map...")
            with contextlib.suppress(Exception):
                await self._build_clusters()

    # ── Run ──────────────────────────────────────────────────────────
    async def run(self, duration: float = 0.0) -> None:
        from core import telegram

        telegram.set_process_label("SHADOW")
        await self._build_clusters()
        with contextlib.suppress(Exception):
            await telegram.notify_shadow_started(
                len(self.cache._token_ids), len(self.cluster_map)
            )

        tasks = [
            asyncio.create_task(self.cache.run()),
            asyncio.create_task(self._consumer()),
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._status_loop()),
            asyncio.create_task(self._refresh_loop()),
        ]
        log.info(
            "Shadow mode running%s. DB=%s",
            f" for {duration:.0f}s" if duration else "",
            CFG.shadow_db_path,
        )
        start = time.time()
        try:
            while self._running:
                await asyncio.sleep(1.0)
                if duration and time.time() - start >= duration:
                    break
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self._running = False
            self.cache.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            n = self.logger.flush_all(self.db)
            log.info(
                "Shadow stopped. Flushed %d. Total evals=%d PASS=%d",
                n,
                self.logger.total(),
                self.logger.passes(),
            )


def main() -> None:
    ap = argparse.ArgumentParser(description="Logical-arb shadow mode")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds to run (0 = until Ctrl-C)")
    args = ap.parse_args()
    setup_logging("shadow", backup_count=30)   # daily rotate at server-local midnight
    app = ShadowApp()
    try:
        asyncio.run(app.run(args.duration))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
