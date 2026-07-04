"""Does ANY live NegRisk market have an ME over-round that beats the REAL fee?

    python3 analysis/niche_scan.py            # scan current NegRisk universe

The fee is min(p,1-p)-weighted, so extreme-priced markets pay almost nothing.
This scans every live NegRisk group, sums the YES outcome prices (the over-round),
subtracts the corrected per-market fee (Σ rate·min(p,1-p)), and reports any group
whose net is positive — i.e. a surviving TAKER niche.

Zero LLM cost (hits Gamma only). CAVEAT: Gamma outcomePrices are last/mid, NOT
executable bids — they OVERSTATE the edge (no slippage, no bid/ask spread). So:
  - net <= 0 on these optimistic prices  => definitively no taker edge (dead).
  - net  > 0 here                        => a CANDIDATE, must be re-checked against
                                            real book depth before believing it.
"""
import json
import urllib.request
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
GAMMA = "https://gamma-api.polymarket.com"


def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))


def parse_prices(m):
    p = m.get("outcomePrices")
    try:
        p = json.loads(p) if isinstance(p, str) else p
        return [float(x) for x in (p or [])]
    except Exception:
        return []


def market_rate(m):
    sched = m.get("feeSchedule")
    if isinstance(sched, dict):
        try:
            return float(sched.get("rate") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def main():
    groups = defaultdict(list)      # negRiskMarketID -> list of (yes_price, rate)
    names = {}
    pages = 0
    for pg in range(60):            # up to 6000 markets
        page = get(f"{GAMMA}/markets?active=true&closed=false&archived=false&limit=100&offset={pg*100}")
        if not page:
            break
        pages += 1
        for m in page:
            if not (m.get("enableNegRisk") or m.get("negRisk")):
                continue
            gid = m.get("negRiskMarketID") or m.get("negRiskMarketId")
            if not gid:
                continue
            pr = parse_prices(m)
            if not pr:
                continue
            groups[gid].append((pr[0], market_rate(m)))     # YES = first outcome
            names.setdefault(gid, m.get("question") or m.get("groupItemTitle") or gid)

    print("=" * 72)
    print(f"  NICHE SCAN — {pages} pages, {len(groups)} NegRisk groups")
    print("=" * 72)

    survivors, examined = [], 0
    for gid, legs in groups.items():
        if len(legs) < 2:
            continue
        examined += 1
        over_round_c = (sum(p for p, _ in legs) - 1.0) * 100.0
        fee_c = sum((r or 0.02) * min(p, 1.0 - p) for p, r in legs) * 100.0
        net_c = over_round_c - fee_c
        if over_round_c > 0.5:      # only groups that are actually over-round
            survivors.append((net_c, over_round_c, fee_c, len(legs), names[gid]))

    survivors.sort(reverse=True)
    pos = [s for s in survivors if s[0] > 0]
    print(f"\n  groups examined (>=2 legs): {examined}")
    print(f"  groups with over-round > 0.5c: {len(survivors)}")
    print(f"  NET-POSITIVE after real fee : {len(pos)}")

    print("\n  Top 12 by net (raw over-round vs corrected fee):")
    print(f"    {'net':>7} {'raw':>7} {'fee':>7} {'legs':>5}  market")
    for net, raw, fee, n, q in survivors[:12]:
        flag = "  <-- SURVIVES" if net > 0 else ""
        print(f"    {net:7.2f} {raw:7.2f} {fee:7.2f} {n:5d}  {q[:44]}{flag}")

    print("\n" + "=" * 72)
    if not pos:
        print("VERDICT: no taker niche — even on optimistic Gamma prices, every")
        print("over-round is smaller than the real fee. Taker logical-arb is dead.")
    else:
        print(f"VERDICT: {len(pos)} candidate(s) survive on optimistic prices — verify")
        print("each against real order-book bids (slippage/spread) before believing it.")
    print("=" * 72)


if __name__ == "__main__":
    main()
