"""Main async event loop — paper and live bilateral arbitrage.

Mirrors shadow.py's discovery/cluster/WS architecture but, instead of only
logging telemetry, it executes PASS violations:

    book update → evaluate cluster → for each PASS:
        risk.can_trade() and risk.check_concurrent()
        executor.execute_arb(shadow_trade)   # paper or live (asyncio.gather)

Usage:
  python bot.py                         # paper mode  → arb_trades.db
  python bot.py --portfolio 500         # paper, sizing reference only
  python bot.py --live --confirm-live --accept-risk   # live → arb_live.db
  python bot.py --live ... --db arb_live.db           # explicit live DB

DB selection is mode-locked by core.config.resolve_db_path (Rule 7): paper and
live can never share a file, and neither can be the shadow DB.
"""

import argparse
import asyncio
import contextlib
import logging
import time

from core.config import CFG, resolve_db_path
from core.database import Database
from core.logging_setup import setup_logging
from engine.risk import RiskManager
from engine.signal import ArbEngine
from execution.executor import Executor
from feeds.clustering import MarketClusterer
from feeds.markets import LocalOrderBookCache, MarketDiscovery

log = logging.getLogger("arb.bot")


class ArbBot:
    def __init__(self, is_live: bool, db_path: str) -> None:
        self.is_live = is_live
        self.db = Database(db_path)
        self.risk = RiskManager(self.db)
        self.discovery = MarketDiscovery()
        self.clusterer = MarketClusterer()
        self.cluster_map = None
        self._eval_queue: asyncio.Queue = asyncio.Queue()
        self._pending: set[str] = set()
        self.cache = LocalOrderBookCache(on_book_update=self._on_book_update)
        self.executor = self._make_executor()
        # No shadow_logger in bot mode — telemetry stays in the shadow DB only.
        self.engine = ArbEngine(self.cache, cluster_map=None,
                                shadow_logger=None, db=self.db)
        self._running = True

    def _make_executor(self) -> Executor:
        if self.is_live:
            from execution.sdk_executor import SdkExecutor

            return SdkExecutor(self.db, self.cache, is_live=True, cluster_map=None)
        return Executor(self.db, self.cache, is_live=False)

    def _on_book_update(self, token_id: str) -> None:
        if self.cluster_map is None:
            return
        cid = self.cluster_map._token_to_cluster.get(token_id)
        if cid is None or cid in self._pending:
            return
        self._pending.add(cid)
        self._eval_queue.put_nowait(cid)

    async def _build_clusters(self) -> None:
        markets = await self.discovery.get_all_active_markets()
        self.cluster_map = await self.clusterer.build_cluster_map(markets)
        self.engine.cluster_map = self.cluster_map
        if hasattr(self.executor, "cluster_map"):
            self.executor.cluster_map = self.cluster_map
        self.cache.set_token_ids(self.cluster_map.all_token_ids())
        log.info("Clusters: %d  tokens: %d", len(self.cluster_map),
                 len(self.cache._token_ids))

    async def _consumer(self) -> None:
        from core import telegram

        while self._running:
            try:
                cid = await asyncio.wait_for(self._eval_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._pending.discard(cid)
            if cid not in self.cluster_map:
                continue
            cluster = self.cluster_map.get_cluster_for(self.cluster_map[cid][0].token_id)
            if cluster is None:
                continue
            try:
                shadow_trades = await self.engine.evaluate_cluster(cluster)
            except Exception as exc:
                log.debug("evaluate_cluster error: %s", exc)
                continue
            for st in shadow_trades:
                if st.reason != "PASS":
                    continue
                ok, why = self.risk.can_trade()
                if not ok:
                    log.warning("Skip PASS — risk: %s", why)
                    with contextlib.suppress(Exception):
                        await telegram.notify_kill_switch(why, self.db.daily_pnl())
                    continue
                if not self.risk.check_concurrent():
                    log.info("Skip PASS — max concurrent arbs reached")
                    continue
                with contextlib.suppress(Exception):
                    result = await self.executor.execute_arb(st)
                    if result and result.trade.status in ("OPEN", "CLOSED"):
                        await telegram.notify_trade(result.trade, opened=True)

    async def _maintenance(self) -> None:
        while self._running:
            await asyncio.sleep(CFG.cluster_refresh_interval_sec)
            log.info("Refreshing clusters...")
            with contextlib.suppress(Exception):
                await self._build_clusters()

    async def run(self, portfolio: float) -> None:
        from core import telegram

        telegram.set_process_label("LIVE" if self.is_live else "PAPER")
        await self._build_clusters()
        log.info("ArbBot %s mode | DB=%s | portfolio ref $%.0f",
                 "LIVE" if self.is_live else "PAPER", self.db.path, portfolio)

        tasks = [
            asyncio.create_task(self.cache.run()),
            asyncio.create_task(self._consumer()),
            asyncio.create_task(self._maintenance()),
        ]
        try:
            while self._running:
                await asyncio.sleep(1.0)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self._running = False
            self.cache.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.is_live and hasattr(self.executor, "aclose"):
                await self.executor.aclose()
            stats = self.db.lifetime_stats()
            log.info("Stopped. trades=%d wr=%.1f%% pnl=$%+.4f unwind=%.1f%%",
                     stats["total"], stats["wr"], stats["pnl"], stats["unwind_rate"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Logical-arb bot (paper/live)")
    ap.add_argument("--portfolio", type=float, default=100.0,
                    help="portfolio reference for sizing/telemetry")
    ap.add_argument("--live", action="store_true", help="LIVE trading")
    ap.add_argument("--confirm-live", action="store_true",
                    help="required acknowledgement for --live")
    ap.add_argument("--accept-risk", action="store_true",
                    help="required acknowledgement for --live")
    ap.add_argument("--db", default="", help="override trades DB (mode-guarded)")
    args = ap.parse_args()

    setup_logging("bot", backup_count=90)   # daily rotate at server-local midnight

    if args.live and not (args.confirm_live and args.accept_risk):
        raise SystemExit(
            "LIVE requires --confirm-live AND --accept-risk. Refusing to start.\n"
            "Go-live is gated on optimize.py → GO LIVE first (CLAUDE.md Rule 3)."
        )

    db_path = resolve_db_path(args.live, args.db)   # raises if modes would mix
    bot = ArbBot(is_live=args.live, db_path=db_path)
    try:
        asyncio.run(bot.run(args.portfolio))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
