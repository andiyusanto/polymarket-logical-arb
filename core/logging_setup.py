"""Daily-rotating file logging (server-local midnight) + console.

Ported from the bear bot's _setup_logging. Logs are written to
  logs/<YYYY-MM-DD>_<component>.log
and roll over at midnight in the SERVER's local timezone (utc=False — per
operator request: a new file each calendar day on the box the bot runs on).
Rotated files keep their date in the name; backup_count days are retained, older
ones are pruned automatically.

NOTE: this controls only the log *file* cadence. Trading-day logic (daily loss
cap reset, daily_pnl) deliberately stays on UTC in core/database.py — those are
accounting boundaries, not operator-readability boundaries. They are independent
on purpose; don't unify them.
"""

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

from core.config import CFG


def setup_logging(component: str, backup_count: int = 30,
                  level: int = logging.INFO) -> str:
    """Install a midnight-rotating file handler + console handler on the root
    logger. Returns the active log path. Idempotent (safe to call once per
    process; clears pre-existing handlers so re-entry won't double-log)."""
    logs_dir = Path(CFG.log_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")   # local = server timezone
    log_path = logs_dir / f"{today}_{component}.log"

    fh = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        backupCount=backup_count,
        encoding="utf-8",
        utc=False,            # rotate at SERVER-local midnight, not UTC
    )

    def _namer(default_name: str) -> str:
        # default rotated name is "<base>.YYYY-MM-DD"; rename to keep the
        # date-first convention: "<YYYY-MM-DD>_<component>.log".
        _base, date_suffix = default_name.rsplit(".", 1)
        return str(logs_dir / f"{date_suffix}_{component}.log")

    fh.namer = _namer

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(sh)

    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "hpack", "h2",
                  "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return str(log_path)
