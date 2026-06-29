# polymarket-logical-arb

**Polymarket Temporal & Conditional Arbitrage Bot** — detects and trades *logical*
arbitrage across related prediction markets by enforcing mathematical probability
constraints. The edge is logical necessity, not probability estimation: when two
markets have a genuine deterministic relationship and their prices violate it, a
(near) risk-free spread exists.

It enforces three constraints:

| Type | Rule | Example |
|------|------|---------|
| **Mutually exclusive** | Σ P(outcomes) ≤ 1.00 within one NegRisk event | candidates A+B+C YES prices summing to 1.10 |
| **Temporal monotonicity** | P("X by Sept") ≤ P("X by Dec") | earlier deadline can't beat later |
| **Threshold monotonicity** | P("BTC > $100k") ≤ P("BTC > $90k") | higher bar can't beat lower |

Mutually-exclusive is prioritized — it's the most-proven type ($29M of ~$40M
historical multi-condition profit), it's a pure accounting identity within one
NegRisk event, and it needs **no LLM** (deterministic NegRisk grouping).

This is **not** a speed bot (execution window is seconds), **not** a single-market
YES+NO rebalancer, **not** a directional oracle sniper, and **not** statistical
arbitrage. The moat is semantic analysis quality, not latency.

> The authoritative development rulebook is [CLAUDE.md](CLAUDE.md). Read it before
> changing any threshold or logic. The original build spec is [OPUS_PROMPT.md](OPUS_PROMPT.md).

---

## Architecture

```
WebSocket book update (token_id)
        ↓
LocalOrderBookCache.update()          ← single source of truth for all prices
        ↓
ClusterMap.get_cluster_for(token_id)  ← NegRisk groups + LLM-detected pairs
        ↓
ArbEngine.evaluate_cluster()
   ├── constraints: temporal / threshold / mutually-exclusive
   └── gate ladder G1–G7 (ambiguity, spread, depth, concurrency, blackout)
        ↓
   reason == "PASS" ─┬─ shadow → ShadowLogger → shadow_run.db
                     └─ paper/live → Executor.execute_arb()  (asyncio.gather, both legs)
```

Every gate failure is recorded with its reason so shadow telemetry shows exactly
where signals die. Constraint checks read only from the in-memory cache — never
REST on the hot path.

---

## Repository layout

```
core/      config.py models.py database.py shadow.py telegram.py logging_setup.py
feeds/     markets.py (discovery + WS book cache)   clustering.py (LLM + NegRisk)
engine/    constraints.py  ambiguity.py  signal.py  risk.py
execution/ executor.py (paper)            sdk_executor.py (live, bilateral)
analysis/  shadow_report.py  optimize.py  analyze.py
ui/        dashboard.py
deploy/    arb-shadow.service
bot.py shadow.py setup.py get_creds.py
```

---

## Setup

Python 3.11+. Two virtualenvs (mirrors the bear bot's `~/arbvenv` / `~/arblive`):

```bash
# shadow + paper (no live SDK)
python3 -m venv ~/logicvenv
~/logicvenv/bin/pip install -r requirements.txt

# live (adds polymarket-client deposit-wallet SDK)
python3 -m venv ~/logiclive
~/logiclive/bin/pip install -r requirements-sdk.txt
```

`sentence-transformers` (embedding-clustering fallback) is **optional and heavy**
(pulls torch); clustering uses Anthropic as the primary path. Install it only if
you want the offline fallback.

Credentials:

```bash
cp .env.example .env          # fill in wallet + ANTHROPIC_API_KEY
python setup.py               # derives Polymarket API creds into .env
```

Required env: `POLY_PRIVATE_KEY`, `POLY_FUNDER_ADDRESS`, `POLY_SIG_TYPE`,
`ANTHROPIC_API_KEY` (LLM pairing/ambiguity). Live adds
`BUILDER_API_KEY/SECRET/PASSPHRASE`. Telegram is optional.

---

## Usage

### Shadow mode (zero trades — start here)
```bash
python shadow.py                 # run until Ctrl-C
python shadow.py --duration 1800 # run 30 min then exit cleanly
```

### Paper / live
```bash
python bot.py                                       # paper   → arb_trades.db
python bot.py --portfolio 500                       # paper, sizing reference
python bot.py --live --confirm-live --accept-risk   # live    → arb_live.db
```
Live refuses to start without **both** `--confirm-live` and `--accept-risk`, and
only after `optimize.py` outputs `→ GO LIVE` (Rule 3).

### Analysis & monitoring
```bash
python analysis/shadow_report.py            # gate breakdown, spread decay, PASS signals
python analysis/optimize.py --min-days 14   # the GO / NO-GO decision + recommended config
python analysis/analyze.py --paper          # paper trade stats (or --live / --watch)
python ui/dashboard.py                       # live Rich dashboard (shadow_run.db; --trades / --db)
```

---

## Database separation (Rule 7 — enforced)

Three strictly separate SQLite files, one per mode. The risk manager (daily loss
cap, rolling-WR halt) does **not** filter by mode, so mixing modes corrupts the
kill switch. `core.config.resolve_db_path()` refuses to cross modes — live can
never use the paper DB, paper can never use the live DB, neither can be the shadow DB.

| Mode | Command | DB |
|------|---------|----|
| Shadow | `python shadow.py` | `shadow_run.db` |
| Paper | `python bot.py` | `arb_trades.db` |
| Live | `python bot.py --live …` | `arb_live.db` |

All `*.db`, `logs/`, and `cluster_review.log` are gitignored.

---

## Logging

Daily-rotating files via `core/logging_setup.py`, rolling at **server-local
midnight** (`utc=False`):

```
logs/<YYYY-MM-DD>_shadow.log    (30-day retention)
logs/<YYYY-MM-DD>_bot.log       (90-day retention)
```

Under systemd, console output goes to journald (which self-rotates) — see
[deploy/arb-shadow.service](deploy/arb-shadow.service). Note: log rotation uses
server-local time, but trading-day accounting (daily loss cap, `daily_pnl`) stays
on **UTC** — these boundaries are independent on purpose.

---

## Execution rule (non-negotiable)

Both legs of an arb are submitted **simultaneously** via `asyncio.gather()` —
sequential submission introduces legging risk that defeats the strategy. Every
leg is a BUY (a "SELL" leg buys the complementary NO token; a mutually-exclusive
basket buys the NO of every outcome). On a partial fill, the filled legs are
unwound within `unwind_max_sec` (30s) and logged as `UNWOUND`.

> Scope note: realizing a *fully-filled* arb's profit needs an on-chain
> redemption module at market resolution (like the bear bot's redeem path). That
> settlement step is **not** in this build — do not deploy real capital until it
> is wired. Until then, paper PnL models the locked spread and live books filled
> arbs as `OPEN`.

---

## Path to go-live

This is a measurement process, not a switch. Do not skip phases (CLAUDE.md).

1. **Shadow ≥ 14 days** — `arb-shadow.service`, meet the shadow-mode minimum criteria.
2. **`optimize.py --min-days 14`** — must output `→ GO LIVE`. It only *recommends*
   config; a human reviews and edits `core/config.py` manually, logging the change
   in CLAUDE.md's Config Change Log.
3. **Paper ≥ 30 closed trades** — meet the paper exit criteria (WR, unwind rate,
   spread).
4. **Live micro → full** — start at `base_position_usdc = $10`, scale only on
   measured unwind rate < 10% and realized spread > 1.5¢.

Thresholds in `core/config.py` are starting points from cross-market arbitrage
literature; shadow data may reveal different natural parameters for your market mix.

---

## Reference

Built from [OPUS_PROMPT.md](OPUS_PROMPT.md), patterned on the read-only bear bot
(`~/polymarket-arbitrage-bot/bear-oracle-confirmed-sniper`) for SDK signing,
shadow architecture, WebSocket management, and gate philosophy. Empirical basis:
arXiv:2508.03474 (IMDEA), arXiv:2605.00864 (UCLA).
