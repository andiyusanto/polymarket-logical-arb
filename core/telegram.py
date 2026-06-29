"""Telegram notifications for the logical-arb bot (adapted from the bear bot).

Fails silently when credentials are missing. A process label (SHADOW/PAPER/LIVE)
tags messages so the shared channel stays readable.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

from core.config import CFG

log = logging.getLogger("arb.telegram")

_MIN_INTERVAL = 1.0
_last_send_ts = 0.0
_PROCESS_LABEL = ""


def set_process_label(label: str) -> None:
    global _PROCESS_LABEL
    _PROCESS_LABEL = label


def is_configured() -> bool:
    return bool(CFG.telegram_token and CFG.telegram_chat_id)


async def send(text: str, parse_mode: str = "HTML", _retries: int = 3) -> bool:
    global _last_send_ts
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage"
    payload = {
        "chat_id": CFG.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    for attempt in range(_retries):
        now = time.time()
        if now - _last_send_ts < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - (now - _last_send_ts))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    _last_send_ts = time.time()
                    if resp.status == 200:
                        return True
                    if resp.status == 429:
                        await asyncio.sleep(int(resp.headers.get("Retry-After", 5)))
                        continue
        except Exception as exc:
            log.warning("Telegram send error (%d/%d): %s", attempt + 1, _retries, exc)
        if attempt < _retries - 1:
            await asyncio.sleep(2.0 * (attempt + 1))
    return False


def _tag() -> str:
    return f"[{_PROCESS_LABEL}] " if _PROCESS_LABEL else ""


async def notify_shadow_started(n_markets: int, n_clusters: int) -> bool:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return await send(
        f"👁 <b>{_tag()}ARB SHADOW STARTED</b> — logical-arb\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Time: {ts}\nMarkets: <b>{n_markets}</b>  Clusters: <b>{n_clusters}</b>\n"
        f"Zero trades — constraint + gate telemetry only"
    )


async def notify_shadow_pass(st) -> bool:
    v = st.violation
    p = v.pair
    return await send(
        f"✅ <b>{_tag()}SHADOW PASS</b> ({v.violation_type.value})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"A: {p.market_a.question[:80]}\nB: {p.market_b.question[:80]}\n"
        f"Spread: <b>{v.estimated_spread_after_slippage_cents:.2f}¢</b> "
        f"(raw {v.raw_spread_cents:.2f}¢)\n"
        f"Tier: <b>{st.confidence_tier.value}</b>  Size: ${st.intended_size_usdc:.0f}"
    )


async def notify_trade(trade, opened: bool = True) -> bool:
    if opened:
        return await send(
            f"📝 <b>[{trade.mode}] ARB OPENED</b> ({trade.violation_type})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{trade.leg_a_side} {trade.market_a_token[:10]} / "
            f"{trade.leg_b_side} {trade.market_b_token[:10]}\n"
            f"Spread: {trade.entry_spread_cents:.2f}¢  Size: ${trade.size_usdc:.0f}\n"
            f"Tier: {trade.confidence_tier}  ID: <code>{trade.id}</code>"
        )
    won = trade.pnl_usdc > 0
    return await send(
        f"{'✅' if won else '❌'} <b>[{trade.mode}] ARB {trade.status}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"P&L: <b>${trade.pnl_usdc:+.4f}</b>  ID: <code>{trade.id}</code>"
    )


async def notify_kill_switch(reason: str, daily_pnl: float) -> bool:
    return await send(
        f"🚨 <b>{_tag()}KILL SWITCH</b> — logical-arb\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Reason: {reason}\nDaily P&L: <b>${daily_pnl:+.4f}</b>\n"
        f"⚠️ Trading paused until next UTC day"
    )
