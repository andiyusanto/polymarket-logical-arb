"""Risk manager: daily loss cap, rolling-WR halt, concurrency + unwind limits.

Adapted from the bear bot's engine/risk.py for the arb bot's config names. The
manager reads from ONE trades DB (paper or live, never shadow) — Rule 7 keeps the
kill switch reading a single coherent trade stream.
"""

import logging
from datetime import datetime, timezone

from core.config import CFG
from core.database import Database

log = logging.getLogger("arb.risk")


class RiskManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.kill_switch = False
        self._last_day = ""
        self._check_day()

    def _check_day(self) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_day:
            self._last_day = today
            self.kill_switch = False  # reset at UTC midnight

    def can_trade(self) -> tuple[bool, str]:
        self._check_day()
        if self.kill_switch:
            return False, "kill switch active"

        daily_pnl = self.db.daily_pnl()
        if daily_pnl < 0 and abs(daily_pnl) >= CFG.daily_loss_cap_usdc:
            self.kill_switch = True
            log.critical("KILL SWITCH: daily loss $%.2f >= cap $%.2f",
                         abs(daily_pnl), CFG.daily_loss_cap_usdc)
            return False, f"daily loss ${abs(daily_pnl):.2f} >= cap"

        wr = self.db.rolling_wr(CFG.rolling_wr_window)
        if wr is not None and wr < CFG.rolling_wr_halt_threshold:
            log.critical("HALT: rolling WR %.1f%% < %.1f%% over last %d",
                         wr * 100, CFG.rolling_wr_halt_threshold * 100,
                         CFG.rolling_wr_window)
            return False, f"WR {wr * 100:.1f}% < {CFG.rolling_wr_halt_threshold * 100:.1f}%"

        # Unwind-rate circuit (CLAUDE.md: >20% over 10 trades = systemic).
        uw = self.db.unwind_rate(10)
        if uw is not None and uw > 0.20:
            log.critical("HALT: unwind rate %.0f%% > 20%% over last 10 — investigate",
                         uw * 100)
            return False, f"unwind rate {uw * 100:.0f}% > 20%"

        return True, "ok"

    def check_concurrent(self) -> bool:
        return self.db.active_arb_count() < CFG.max_concurrent_arbs
