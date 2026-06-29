"""Configuration for the Polymarket Temporal & Conditional Arbitrage Bot.

Single source of truth for every threshold (same CFG-dataclass pattern as the
bear bot's core/config.py). Nothing in the engine hard-codes a number — it all
flows from here, and every change to a value must be logged in CLAUDE.md's
Config Change Log with the analysis output that justifies it (Rule 2).

The go-live reference thresholds (golive_*) are NOT hard limits — they come from
cross-market arbitrage literature. analysis/optimize.py recommends adjusted
values from actual shadow data; the human operator makes the final call.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.split("#", 1)[0].strip().strip("'\""))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    # ── Credentials ─────────────────────────────────────────────────
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    api_key: str = os.getenv("POLY_API_KEY", "")
    api_secret: str = os.getenv("POLY_API_SECRET", "")
    api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    funder_address: str = os.getenv("POLY_FUNDER_ADDRESS", "")
    sig_type: int = int(os.getenv("POLY_SIG_TYPE", "0") or "0")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Endpoints ───────────────────────────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # ── LLM model (Anthropic) ───────────────────────────────────────
    # Latest capable model for pair + ambiguity reasoning.
    llm_model: str = "claude-opus-4-8"
    llm_max_tokens: int = 4096

    # ── Market discovery ────────────────────────────────────────────
    min_market_volume_usd: float = 5000.0
    market_refresh_interval_sec: int = 3600
    book_refresh_interval_sec: int = 5
    gamma_page_limit: int = 500          # markets per Gamma page request
    max_markets_monitored: int = 1500    # safety cap on WS subscriptions

    # ── Clustering ──────────────────────────────────────────────────
    cluster_refresh_interval_sec: int = 3600
    cluster_eps: float = 0.3             # DBSCAN fallback eps (LLM is primary)
    llm_pair_batch_size: int = 50        # markets per LLM call
    llm_confidence_threshold: float = 0.7

    # ── Constraint engine ───────────────────────────────────────────
    min_violation_spread_cents: float = 1.0   # detect above this
    noise_threshold_cents: float = 1.0        # ignore violations below this

    # ── Ambiguity detection ─────────────────────────────────────────
    structural_score_threshold: float = 0.7
    semantic_score_threshold: float = 0.85    # >=this HIGH; >=0.70 MEDIUM

    # ── Execution ───────────────────────────────────────────────────
    base_position_usdc: float = 50.0
    position_multiplier_high: float = 1.0     # confidence tier HIGH
    position_multiplier_med: float = 0.5      # confidence tier MEDIUM
    min_order_shares: int = 5                 # Polymarket minimum
    min_book_depth_usd: float = 20.0          # G5: both legs need this depth
    cooldown_sec: float = 0.5                 # min seconds between order bursts

    # ── Risk management ─────────────────────────────────────────────
    max_concurrent_arbs: int = 3
    daily_loss_cap_usdc: float = 25.0
    rolling_wr_halt_threshold: float = 0.45   # halt if WR below this
    rolling_wr_window: int = 20
    unwind_max_sec: float = 30.0              # max time to hold a one-legged pos

    # ── Shadow mode go-live reference thresholds (Layer 1 hard gates) ─
    golive_min_spread_cents: float = field(
        default_factory=lambda: _env_float("ARB_MIN_SPREAD_CENTS", 2.0)
    )
    golive_min_opp_per_day: float = field(
        default_factory=lambda: _env_float("ARB_MIN_OPP_PER_DAY", 5.0)
    )
    golive_min_duration_sec: float = field(
        default_factory=lambda: _env_float("ARB_MIN_DURATION_SEC", 15.0)
    )
    golive_min_shadow_days: int = 14          # minimum observation period

    # ── Fees ────────────────────────────────────────────────────────
    # Polymarket V2 taker fee. NegRisk / multi-outcome markets and the
    # crypto books differ; 2% is the conservative reference used in spread
    # math. Maker/limit orders are free. Tune per actual market mix.
    polymarket_taker_fee: float = 0.02

    # ── Follow-up poll schedule (shadow telemetry) ──────────────────
    followup_poll_secs: tuple = (5.0, 15.0, 30.0, 60.0)

    # ── Infrastructure ──────────────────────────────────────────────
    # Rule 7: three STRICTLY separate SQLite files, one per mode. The risk
    # manager (daily loss cap, rolling-WR halt) does NOT filter by mode, so
    # mixing modes in one file corrupts the kill switch. bot.py / shadow.py
    # select the file by mode and refuse to cross modes (see resolve_db_path).
    shadow_db_path: str = "shadow_run.db"   # shadow.py only
    db_path: str = "arb_trades.db"          # paper (bot.py, no --live)
    live_db_path: str = "arb_live.db"       # live  (bot.py --live)
    log_dir: str = "logs"
    poll_interval: float = 0.1                # main loop sleep
    shadow_flush_interval_sec: float = 30.0   # batch flush cadence

    # ── Blackout hours (UTC) — populated from shadow optimize.py ─────
    blackout_hours: set = field(default_factory=set)


CFG = Config()


def resolve_db_path(is_live: bool, override: str = "") -> str:
    """Pick the trades DB for the mode and refuse to cross modes (Rule 7).

    Paper → arb_trades.db, live → arb_live.db, and neither may ever be the
    shadow DB. An explicit --db override is honoured but still guarded so a
    typo can't point live trading at the paper history (which would let the
    risk manager's kill switch read the wrong trade stream).
    """
    default = CFG.live_db_path if is_live else CFG.db_path
    path = override or default
    if path == CFG.shadow_db_path:
        raise ValueError(
            f"Refusing to use the shadow DB ({path}) for trading (Rule 7)."
        )
    if is_live and path == CFG.db_path:
        raise ValueError(
            "Refusing to run LIVE against the paper DB; use arb_live.db (Rule 7)."
        )
    if not is_live and path == CFG.live_db_path:
        raise ValueError(
            "Refusing to run PAPER against the live DB; use arb_trades.db (Rule 7)."
        )
    return path
