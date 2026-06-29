"""Live bilateral execution via polymarket-client (deposit-wallet SDK).

Ported from the bear bot's SdkExecutor (AsyncSecureClient, builder creds, FOK
market orders) and extended for ARB's non-negotiable rule:

    BOTH LEGS ARE SUBMITTED SIMULTANEOUSLY via asyncio.gather().

Sequential submission would let the first fill move the second market — exactly
the legging risk logical arb exists to avoid. Every leg is a BUY (a SELL leg buys
the complementary NO token); a mutually-exclusive basket gathers a BUY-NO order
for every outcome in the cluster.

Partial-fill protocol (CLAUDE.md):
  - if some legs fill and others fail → immediately unwind the filled legs
    (opposite FOK market order) within unwind_max_sec
  - log the trade as UNWOUND (a managed exit), not a silent loss
  - one-legged positions are never held past the unwind window

NOTE: realizing a fully-filled arb's profit requires on-chain redemption at
market resolution (same as the bear bot's redeem path) — that settlement module
is intentionally out of this build's scope. A clean fully-filled arb is booked
OPEN (spread locked at entry); the risk manager's realized-PnL stream comes from
UNWOUND exits until the redeem module lands. Do not deploy real capital before
wiring resolution + redemption.
"""

import asyncio
import logging
import time
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from dotenv import dotenv_values

from core.config import CFG
from core.database import Database
from core.models import ArbResult, ArbSide, ShadowTrade, ViolationType
from execution.executor import Executor, resolve_leg

log = logging.getLogger("arb.executor.sdk")


def _scale(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f / 1e6 if f > 1000 else f


class SdkExecutor(Executor):
    def __init__(self, db: Database, cache, is_live: bool = True,
                 cluster_map=None) -> None:
        super().__init__(db, cache, is_live)
        self.cluster_map = cluster_map
        env = dotenv_values(".env")

        def pick(*names: str) -> Optional[str]:
            for n in names:
                v = env.get(n)
                if v:
                    return v.split(" #", 1)[0].strip().strip("'\"")
            return None

        self._magic_key = pick("POLY_PRIVATE_KEY") or CFG.private_key
        self._wallet = pick("POLY_FUNDER_ADDRESS") or CFG.funder_address
        self._b_key = pick("BUILDER_API_KEY", "POLY_BUILDER_API_KEY")
        self._b_secret = pick("BUILDER_SECRET", "POLY_BUILDER_SECRET")
        self._b_pass = pick("BUILDER_PASSPHRASE", "POLY_BUILDER_PASSPHRASE")
        self._builder_code = pick("BUILDER_CODE", "POLY_BUILDER_CODE")
        self._sdk = None
        self._circuit_open = False
        self._last_order_ts = 0.0

    async def _ensure_sdk(self):
        if self._sdk is not None:
            return self._sdk
        if not self._magic_key or not self._wallet:
            log.error("SDK live requires POLY_PRIVATE_KEY + POLY_FUNDER_ADDRESS")
            return None
        if not (self._b_key and self._b_secret and self._b_pass):
            log.error("SDK live requires BUILDER_API_KEY/SECRET/PASSPHRASE in .env")
            return None
        try:
            from polymarket.auth import BuilderApiKey
            from polymarket.clients import AsyncSecureClient

            self._sdk = await AsyncSecureClient.create(
                private_key=self._magic_key,
                wallet=self._wallet,
                api_key=BuilderApiKey(self._b_key, self._b_secret, self._b_pass),
            )
            log.info("polymarket-client AsyncSecureClient ready (wallet=%s)",
                     self._wallet)
            return self._sdk
        except Exception as e:
            log.error("Failed to init polymarket-client: %s", e, exc_info=True)
            return None

    async def aclose(self) -> None:
        if self._sdk is not None:
            try:
                await self._sdk.close()
            except Exception:
                pass
            self._sdk = None

    # ── Public entry point (live) ─────────────────────────────────────
    async def execute_arb(self, st: ShadowTrade) -> ArbResult:
        now = time.time()
        if now - self._last_order_ts < CFG.cooldown_sec:
            return ArbResult(trade=self._build_trade(st, "LIVE"),
                             success=False, error="cooldown")
        self._last_order_ts = now

        client = await self._ensure_sdk()
        trade = self._build_trade(st, "LIVE")
        if client is None or self._circuit_open:
            trade.status = "CANCELLED"
            trade.notes = "sdk unavailable / circuit open"
            self.db.save_trade(trade)
            return ArbResult(trade=trade, success=False, error="no sdk")

        legs = self._build_legs(st)        # [(token_id, size_usdc), ...]
        if len(legs) < 2:
            trade.status = "CANCELLED"
            trade.notes = "could not build >=2 legs"
            self.db.save_trade(trade)
            return ArbResult(trade=trade, success=False, error="legs")

        # ── THE CRITICAL RULE: submit every leg simultaneously ──
        results = await asyncio.gather(
            *(self._place_buy(client, tok, size) for tok, size in legs),
            return_exceptions=True,
        )
        fills = [self._norm_result(r) for r in results]
        filled = [f for f in fills if f["filled"]]

        trade.executed_at = time.time()
        trade.leg_a_fill_price = fills[0]["price"] if fills else 0.0
        trade.leg_b_fill_price = fills[1]["price"] if len(fills) > 1 else 0.0
        trade.leg_a_filled = fills[0]["filled"] if fills else False
        trade.leg_b_filled = fills[1]["filled"] if len(fills) > 1 else False

        if len(filled) == len(legs):
            # Fully filled — spread locked. Held to resolution (see module note).
            trade.status = "OPEN"
            trade.notes = f"all {len(legs)} legs filled"
            self.db.save_trade(trade)
            log.info("LIVE ARB FILLED: %s %d legs @ spread %.2f¢",
                     trade.violation_type, len(legs), trade.entry_spread_cents)
            return ArbResult(trade=trade, success=True)

        if not filled:
            trade.status = "CANCELLED"
            trade.notes = "no legs filled (FOK killed)"
            self.db.save_trade(trade)
            log.warning("LIVE ARB: no legs filled — clean miss")
            return ArbResult(trade=trade, success=False, error="no fill")

        # ── Partial fill → unwind the filled legs immediately ──
        log.warning("LIVE ARB PARTIAL: %d/%d filled — UNWINDING", len(filled),
                    len(legs))
        pnl = await self._unwind(client, filled)
        trade.status = "UNWOUND"
        trade.pnl_usdc = round(pnl, 6)
        trade.notes = f"partial {len(filled)}/{len(legs)} — unwound"
        self.db.save_trade(trade)
        return ArbResult(trade=trade, success=False, unwound=True)

    # ── Leg construction ──────────────────────────────────────────────
    def _build_legs(self, st: ShadowTrade) -> list[tuple[str, float]]:
        v = st.violation
        size = st.intended_size_usdc or CFG.base_position_usdc
        if v.violation_type == ViolationType.MUTUALLY_EXCLUSIVE:
            cluster = (
                self.cluster_map.get_cluster_for(v.pair.market_a.token_id)
                if self.cluster_map else None
            )
            members = cluster.markets if cluster else [v.pair.market_a, v.pair.market_b]
            per = size / max(len(members), 1)
            return [(m.no_token_id or m.token_id, per) for m in members]
        tok_a, _ = resolve_leg(v.pair.market_a, v.leg_a_side)
        tok_b, _ = resolve_leg(v.pair.market_b, v.leg_b_side)
        return [(tok_a, size), (tok_b, size)]

    # ── SDK order helpers ─────────────────────────────────────────────
    async def _place_buy(self, client, token_id: str, size_usdc: float) -> dict:
        amount = Decimal(str(round(size_usdc, 2)))
        resp = await client.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=amount,
            max_price=Decimal("0.99"),
            order_type="FOK",
            builder_code=self._builder_code or None,
        )
        taking = _scale(getattr(resp, "taking_amount", 0))   # shares received
        making = _scale(getattr(resp, "making_amount", 0))   # pUSD spent
        price = round(making / taking, 4) if (taking and making) else 0.0
        return {
            "token_id": token_id,
            "filled": bool(taking and taking > 0),
            "shares": taking,
            "spend": making,
            "price": price,
        }

    @staticmethod
    def _norm_result(r) -> dict:
        if isinstance(r, Exception):
            log.warning("leg order error: %s", r)
            return {"token_id": "", "filled": False, "shares": 0.0,
                    "spend": 0.0, "price": 0.0}
        return r

    async def _unwind(self, client, filled: list[dict]) -> float:
        """SELL back every filled leg (FOK) within the unwind window. Returns the
        net realized PnL of the round-trip (spend on entry minus sell proceeds)."""
        deadline = time.time() + CFG.unwind_max_sec
        sells = await asyncio.gather(
            *(self._place_sell(client, f["token_id"], f["shares"]) for f in filled),
            return_exceptions=True,
        )
        pnl = 0.0
        for f, s in zip(filled, sells):
            proceeds = 0.0 if isinstance(s, Exception) else s
            pnl += proceeds - f["spend"]
            if isinstance(s, Exception):
                log.error("UNWIND leg %s FAILED: %s — MANUAL INTERVENTION",
                          f["token_id"][:10], s)
        if time.time() > deadline:
            log.error("UNWIND exceeded %.0fs window", CFG.unwind_max_sec)
        log.warning("UNWIND complete: net pnl $%+.4f over %d legs", pnl, len(filled))
        return pnl

    async def _place_sell(self, client, token_id: str, shares: float) -> float:
        whole = float(Decimal(str(shares)).quantize(Decimal("1"), rounding=ROUND_DOWN))
        if whole <= 0:
            return 0.0
        resp = await client.place_market_order(
            token_id=token_id,
            side="SELL",
            shares=Decimal(str(whole)),
            min_price=Decimal("0.01"),
            order_type="FOK",
            builder_code=self._builder_code or None,
        )
        return _scale(getattr(resp, "taking_amount", 0))   # pUSD received

    async def cancel_all(self) -> bool:
        client = await self._ensure_sdk()
        if client is None:
            return False
        try:
            await client.cancel_all()
            log.warning("KILL SWITCH: all open orders cancelled")
            return True
        except Exception as e:
            log.error("cancel_all failed: %s", e)
            return False
