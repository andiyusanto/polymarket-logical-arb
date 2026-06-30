"""Market discovery + a WebSocket-fed local order-book cache.

Two responsibilities:

  MarketDiscovery       — pulls all active markets from the Gamma API, filters by
                          volume, and surfaces the YES token of each as a
                          MarketInfo (implied prob = YES mid). NegRisk outcomes
                          are grouped via neg_risk_market_id for the
                          mutually-exclusive constraint check.

  LocalOrderBookCache   — the single source of truth for all live prices. Fed by
                          the CLOB market WebSocket; constraint checks read from
                          here and NEVER hit REST directly. Slippage is modelled
                          by walking the cached book levels (estimate_fill_price).

WebSocket reconnect / backoff is ported from the bear bot's feeds/prices.py.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import aiohttp

from core.config import CFG
from core.models import MarketInfo

log = logging.getLogger("arb.markets")


def _parse_dt(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _as_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return []
    return v or []


# ─────────────────────────────────────────────────────────────────────
# Market discovery
# ─────────────────────────────────────────────────────────────────────
class MarketDiscovery:
    def __init__(self) -> None:
        self._last_discovery = 0.0

    def needs_refresh(self) -> bool:
        return time.time() - self._last_discovery > CFG.market_refresh_interval_sec

    async def get_all_active_markets(self) -> list[MarketInfo]:
        """Fetch all active, unresolved markets from Gamma, filtered by volume.

        Returns one MarketInfo per market (the YES outcome token). Pagination
        walks /markets until a short page is returned or the safety cap is hit.
        """
        markets: list[MarketInfo] = []
        seen: set[str] = set()
        offset = 0
        async with aiohttp.ClientSession() as session:
            while len(markets) < CFG.max_markets_monitored and offset < CFG.max_discovery_scan:
                page = await self._fetch_page(session, offset)
                if not page:
                    break
                for m in page:
                    mi = self._to_market_info(m)
                    if mi and mi.token_id not in seen:
                        seen.add(mi.token_id)
                        markets.append(mi)
                if len(page) < CFG.gamma_page_limit:
                    break          # short page → no more markets to scan
                offset += CFG.gamma_page_limit
        self._last_discovery = time.time()
        log.info(
            "Discovery: %d active markets (>= $%.0f volume)",
            len(markets),
            CFG.min_market_volume_usd,
        )
        return markets

    async def _fetch_page(
        self, session: aiohttp.ClientSession, offset: int, max_retries: int = 4
    ) -> list[dict]:
        url = (
            f"{CFG.gamma_url}/markets?active=true&closed=false&archived=false"
            f"&limit={CFG.gamma_page_limit}&offset={offset}"
        )
        for attempt in range(max_retries):
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status in (403, 429):
                        delay = min(2 * (2**attempt), 30)
                        log.warning("Gamma %d at offset %d — backoff %.0fs",
                                    resp.status, offset, delay)
                        await asyncio.sleep(delay)
                        continue
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
                    return data if isinstance(data, list) else data.get("data", [])
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                delay = min(2 * (2**attempt), 30)
                log.debug("Gamma fetch error offset %d: %s — retry %.0fs",
                          offset, exc, delay)
                await asyncio.sleep(delay)
        return []

    def _to_market_info(self, m: dict) -> Optional[MarketInfo]:
        if m.get("closed") or m.get("resolved") or m.get("archived"):
            return None

        volume = m.get("volumeNum")
        if volume is None:
            volume = m.get("volume")
        try:
            volume = float(volume or 0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume < CFG.min_market_volume_usd:
            return None

        tids = _as_list(m.get("clobTokenIds"))
        outcomes = _as_list(m.get("outcomes"))
        prices = _as_list(m.get("outcomePrices"))
        if not tids:
            return None

        # Surface the YES token (index 0, or the outcome literally named "Yes").
        yes_idx = 0
        for i, oc in enumerate(outcomes):
            if str(oc).strip().lower() == "yes":
                yes_idx = i
                break
        if yes_idx >= len(tids):
            return None

        neg_risk = bool(
            m.get("enableNegRisk")
            if m.get("enableNegRisk") is not None
            else (m.get("negRisk") or m.get("neg_risk") or False)
        )
        nr_market_id = (
            m.get("negRiskMarketID")
            or m.get("negRiskMarketId")
            or m.get("neg_risk_market_id")
            or ""
        )

        try:
            ask = float(prices[yes_idx]) if yes_idx < len(prices) else 0.0
        except (TypeError, ValueError):
            ask = 0.0

        no_idx = 1 - yes_idx if len(tids) >= 2 else yes_idx
        mi = MarketInfo(
            token_id=str(tids[yes_idx]),
            no_token_id=str(tids[no_idx]) if no_idx < len(tids) else "",
            question=m.get("question") or m.get("title") or "",
            description=m.get("description") or "",
            end_date=_parse_dt(m.get("endDate") or m.get("end_date_iso")),
            volume_usd=volume,
            condition_id=m.get("conditionId") or m.get("condition_id") or "",
            neg_risk=neg_risk,
            neg_risk_market_id=str(nr_market_id),
            outcome=str(outcomes[yes_idx]) if yes_idx < len(outcomes) else "Yes",
            best_bid=ask,   # seed from Gamma's last price; WS overwrites on connect
            best_ask=ask,
        )
        return mi if mi.question else None


# ─────────────────────────────────────────────────────────────────────
# Local order-book cache (WebSocket-fed)
# ─────────────────────────────────────────────────────────────────────
class _Book:
    __slots__ = ("bids", "asks", "best_bid", "best_ask",
                 "bid_depth_usd", "ask_depth_usd", "depth_usd", "ts")

    def __init__(self) -> None:
        self.bids: list[tuple[float, float]] = []   # (price, size) desc
        self.asks: list[tuple[float, float]] = []   # (price, size) asc
        self.best_bid: float = 0.0
        self.best_ask: float = 0.0
        self.bid_depth_usd: float = 0.0   # top-of-book USD you can SELL into
        self.ask_depth_usd: float = 0.0   # top-of-book USD you can BUY from
        self.depth_usd: float = 0.0       # min(bid, ask) — book_snapshot only, not gated on
        self.ts: float = 0.0


class LocalOrderBookCache:
    """Single source of truth for live prices, fed by the CLOB market WS.

    Constraint checks call get_best_bid_ask() / estimate_fill_price() — both read
    purely from this in-memory cache; neither touches the network.
    """

    def __init__(self, on_book_update: Optional[Callable[[str], None]] = None) -> None:
        self._books: dict[str, _Book] = {}
        self._token_ids: list[str] = []
        self._on_book_update = on_book_update
        self._running = False
        self._reconnects = 0
        self._last_msg_ts = 0.0
        self._ws_silence_timeout = 60

    # ── Read API (cache only) ────────────────────────────────────────
    def get_best_bid_ask(self, token_id: str) -> tuple[float, float]:
        b = self._books.get(token_id)
        if not b:
            return 0.0, 0.0
        return b.best_bid, b.best_ask

    def bid_depth(self, token_id: str) -> float:
        """Top-of-book USD you can SELL into (consume bids)."""
        b = self._books.get(token_id)
        return b.bid_depth_usd if b else 0.0

    def ask_depth(self, token_id: str) -> float:
        """Top-of-book USD you can BUY from (consume asks)."""
        b = self._books.get(token_id)
        return b.ask_depth_usd if b else 0.0

    def side_depth(self, token_id: str, side: str) -> float:
        """Depth on the side a leg actually executes: BUY consumes asks, SELL
        consumes bids. A one-sided book (live bids, empty asks) is fully
        SELL-fillable — `depth()`'s min(bid, ask) would wrongly read it as 0."""
        return self.ask_depth(token_id) if str(side).upper() == "BUY" \
            else self.bid_depth(token_id)

    def is_fresh(self, token_id: str, max_age_sec: float = 30.0) -> bool:
        b = self._books.get(token_id)
        return bool(b and b.ts and (time.time() - b.ts) < max_age_sec)

    def book_snapshot(self, token_id: str) -> dict:
        b = self._books.get(token_id)
        if not b:
            return {"bids": [], "asks": [], "best_bid": 0.0, "best_ask": 0.0,
                    "depth_usd": 0.0, "ts": 0.0}
        return {
            "bids": b.bids[:5],
            "asks": b.asks[:5],
            "best_bid": b.best_bid,
            "best_ask": b.best_ask,
            "depth_usd": b.depth_usd,
            "ts": b.ts,
        }

    def estimate_fill_price(
        self, token_id: str, side: str, size_usdc: float
    ) -> Optional[float]:
        """Average fill price for a market order of size_usdc, walking the cached
        book. side='BUY' consumes asks; side='SELL' consumes bids. Returns None
        if the book is empty (caller treats as un-fillable).
        """
        b = self._books.get(token_id)
        if not b:
            return None
        levels = b.asks if side.upper() == "BUY" else b.bids
        if not levels:
            return None
        remaining = size_usdc
        spent = 0.0
        shares = 0.0
        for price, qty in levels:
            if price <= 0:
                continue
            level_usd = price * qty
            take_usd = min(remaining, level_usd)
            shares += take_usd / price
            spent += take_usd
            remaining -= take_usd
            if remaining <= 1e-9:
                break
        if shares <= 0:
            return None
        if remaining > 1e-6:
            # Book too thin to fill the whole size — price the rest at the
            # worst available level so the slippage estimate is pessimistic.
            worst = levels[-1][0]
            shares += remaining / worst if worst > 0 else 0
            spent += remaining
        return spent / shares if shares > 0 else None

    # ── Write API ────────────────────────────────────────────────────
    def update(
        self,
        token_id: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        b = self._books.get(token_id)
        if b is None:
            b = _Book()
            self._books[token_id] = b
        b.bids = sorted((p, s) for p, s in bids if p > 0 and s > 0)[::-1]
        b.asks = sorted((p, s) for p, s in asks if p > 0 and s > 0)
        b.best_bid = b.bids[0][0] if b.bids else 0.0
        b.best_ask = b.asks[0][0] if b.asks else 0.0
        b.bid_depth_usd = sum(p * s for p, s in b.bids[:3])
        b.ask_depth_usd = sum(p * s for p, s in b.asks[:3])
        b.depth_usd = min(b.bid_depth_usd, b.ask_depth_usd)
        b.ts = time.time()

    # ── WebSocket lifecycle ──────────────────────────────────────────
    def set_token_ids(self, token_ids: list[str]) -> None:
        self._token_ids = list(dict.fromkeys(token_ids))[: CFG.max_markets_monitored]

    async def run(self) -> None:
        """Connect to the CLOB market WS, subscribe to all token_ids, and feed
        the cache. Reconnects with exponential backoff (ported from bear bot)."""
        import websockets

        self._running = True
        while self._running:
            if not self._token_ids:
                await asyncio.sleep(1.0)
                continue
            try:
                async with websockets.connect(
                    CFG.clob_ws,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=None,
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {"type": "market", "assets_ids": self._token_ids}
                        )
                    )
                    log.info("CLOB market WS connected (%d tokens)",
                             len(self._token_ids))
                    self._last_msg_ts = time.time()
                    self._reconnects = 0
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self._ws_silence_timeout
                            )
                            self._last_msg_ts = time.time()
                            self._handle(raw)
                        except asyncio.TimeoutError:
                            log.warning("CLOB WS silent %ds — reconnecting",
                                        self._ws_silence_timeout)
                            break
            except Exception as exc:
                if self._running:
                    self._reconnects += 1
                    delay = min(3 * (2 ** min(self._reconnects - 1, 4)), 60)
                    log.warning("CLOB WS disconnected (%d): %s — retry %.0fs",
                                self._reconnects, exc, delay)
                    await asyncio.sleep(delay)

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        events = msg if isinstance(msg, list) else [msg]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("event_type") or ev.get("type")
            token_id = ev.get("asset_id") or ev.get("token_id")
            if not token_id:
                continue
            if etype == "book":
                bids = [(float(l["price"]), float(l["size"])) for l in ev.get("bids", [])]
                asks = [(float(l["price"]), float(l["size"])) for l in ev.get("asks", [])]
                self.update(token_id, bids, asks)
                self._notify(token_id)
            elif etype == "price_change":
                self._apply_price_change(token_id, ev)
                self._notify(token_id)

    def _apply_price_change(self, token_id: str, ev: dict) -> None:
        b = self._books.get(token_id)
        if b is None:
            b = _Book()
            self._books[token_id] = b
        changes = ev.get("changes") or ev.get("price_changes") or []
        # Single-change messages put price/side/size at the top level.
        if not changes and ev.get("price") is not None:
            changes = [ev]
        bids = dict(b.bids)
        asks = dict(b.asks)
        for ch in changes:
            try:
                price = float(ch["price"])
                size = float(ch["size"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(ch.get("side", "")).upper()
            book = bids if side in ("BUY", "BID") else asks
            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size
        self.update(
            token_id,
            [(p, s) for p, s in bids.items()],
            [(p, s) for p, s in asks.items()],
        )

    def _notify(self, token_id: str) -> None:
        if self._on_book_update:
            try:
                self._on_book_update(token_id)
            except Exception as exc:
                log.debug("on_book_update callback error: %s", exc)

    def stop(self) -> None:
        self._running = False
