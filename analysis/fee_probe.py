"""Probe Polymarket's REAL taker fee on the markets this bot actually trades.

    python3 analysis/fee_probe.py            # sample NegRisk + PASS-list markets
    python3 analysis/fee_probe.py --db shadow_run.prefix.db   # match PASS markets from a DB

Rule 2: we do NOT set core/config.py:polymarket_taker_fee from a guess. This reads
the live fee schedule off real markets and converts it to an effective cents-per-share
so the config change (if any) is backed by data + this script's output.

IMPORTANT — formula caveat: Polymarket charges a SYMMETRIC fee weighted by
min(price, 1-price) (largest near $0.50, ~0 at the extremes). The exact mapping of
the raw fields (takerBaseFee / feeSchedule.rate / exponent) to an effective rate is
NOT fully documented here, so this prints BOTH plausible interpretations and labels
them. Confirm against a real fill receipt before trusting a single number.
"""
import argparse
import json
import sqlite3
import urllib.request
from statistics import mean

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
GAMMA = "https://gamma-api.polymarket.com"

# Keywords from the observed PASS list (CDU Berlin, Waymo, France WC).
PASS_KEYWORDS = ["cdu", "berlin", "waymo", "france", "world cup", "colorado governor"]


def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))


def fee_fields(m):
    return {k: v for k, v in m.items()
            if any(t in k.lower() for t in ("fee", "bps", "rate"))}


def effective_fee_cents(price, taker_base_fee, schedule_rate):
    """Two interpretations of the fee on ONE share at `price`, in cents.
    Polymarket weights the fee by min(price, 1-price)."""
    w = min(price, 1.0 - price)
    # (A) takerBaseFee as basis points: rate = baseFee/10000, weighted
    eff_a = (taker_base_fee / 10000.0) * w * 100.0 if taker_base_fee else None
    # (B) feeSchedule.rate as the fraction, weighted
    eff_b = (schedule_rate * w) * 100.0 if schedule_rate else None
    return w, eff_a, eff_b


def probe_market(m):
    q = (m.get("question") or m.get("title") or "")[:70]
    ff = fee_fields(m)
    tbf = ff.get("takerBaseFee") or ff.get("taker_base_fee")
    sched = ff.get("feeSchedule") or {}
    rate = sched.get("rate") if isinstance(sched, dict) else None
    neg = m.get("enableNegRisk") or m.get("negRisk")
    prices = m.get("outcomePrices")
    try:
        prices = [float(p) for p in (json.loads(prices) if isinstance(prices, str) else prices or [])]
    except Exception:
        prices = []
    print(f"\n  «{q}»   negRisk={bool(neg)} feesEnabled={ff.get('feesEnabled')}")
    print(f"    raw: takerBaseFee={tbf} makerBaseFee={ff.get('makerBaseFee')} "
          f"schedule.rate={rate} rebate={sched.get('rebateRate') if isinstance(sched,dict) else None}")
    # evaluate at the market's live leg prices (or a 0.50 reference)
    test_prices = prices[:3] if prices else [0.50]
    for p in test_prices:
        if not (0 < p < 1):
            continue
        w, a, b = effective_fee_cents(p, tbf, rate)
        astr = f"{a:.2f}c" if a is not None else "n/a"
        bstr = f"{b:.2f}c" if b is not None else "n/a"
        print(f"    @price ${p:.2f} (w=min(p,1-p)={w:.2f}): "
              f"fee/share  (A:bps)={astr}   (B:rate)={bstr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="shadow DB to pull PASS market names from")
    ap.add_argument("--pages", type=int, default=3)
    args = ap.parse_args()

    wanted = list(PASS_KEYWORDS)
    if args.db:
        c = sqlite3.connect(args.db)
        rows = c.execute("SELECT DISTINCT market_a_question FROM shadow_log WHERE reason='PASS'").fetchall()
        c.close()
        wanted += [r[0].lower()[:25] for r in rows if r[0]]
        print(f"[matching against {len(rows)} PASS questions from {args.db}]")

    print("=" * 72)
    print("  FEE PROBE — real Polymarket schedule on the bot's market types")
    print("=" * 72)

    seen_negrisk, matched = [], []
    for pg in range(args.pages):
        off = pg * 100
        try:
            page = get(f"{GAMMA}/markets?active=true&closed=false&archived=false&limit=100&offset={off}")
        except Exception as e:
            print("fetch failed:", e); break
        if not page:
            break
        for m in page:
            q = (m.get("question") or m.get("title") or "").lower()
            if (m.get("enableNegRisk") or m.get("negRisk")) and len(seen_negrisk) < 3:
                seen_negrisk.append(m)
            if any(k in q for k in wanted):
                matched.append(m)

    print("\n--- SAMPLE NegRisk (ME) markets — the type this bot trades ---")
    for m in seen_negrisk:
        probe_market(m)

    print("\n--- MATCHED PASS-list markets (if still live) ---")
    if matched:
        for m in matched[:6]:
            probe_market(m)
    else:
        print("  none of the PASS-list markets are currently active (likely resolved).")

    print("\n" + "=" * 72)
    print("READ ME: (A) treats takerBaseFee as basis points; (B) treats feeSchedule.rate")
    print("as the fraction. Both weighted by min(price,1-price). The effective")
    print("polymarket_taker_fee to log (Rule 2) is fee/share ÷ price at your typical ME")
    print("leg price. Confirm with ONE real fill before trusting a single value.")
    print("=" * 72)


if __name__ == "__main__":
    main()
