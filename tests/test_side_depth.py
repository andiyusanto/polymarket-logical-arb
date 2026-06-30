"""Side-aware book depth + the G5 executable-side gate.

    python tests/test_side_depth.py

A one-sided book (live bids, empty asks) is fully SELL-fillable; the legacy
min(bid,ask) metric collapsed it to 0 and the gate wrongly rejected. Also checks
that ShadowTrade now carries live executable-side depth (not the dead
MarketInfo.depth_usd field). Plain asserts, no pytest dependency.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CFG  # noqa: E402
from core.models import Cluster, MarketInfo  # noqa: E402
from engine.signal import ArbEngine  # noqa: E402
from feeds.markets import LocalOrderBookCache  # noqa: E402


def test_side_aware_depth():
    c = LocalOrderBookCache()
    c.update("one", [(0.40, 100), (0.39, 200)], [])    # one-sided: bids only
    c.update("two", [(0.40, 100)], [(0.42, 100)])      # two-sided
    assert c.bid_depth("one") > 0 and c.ask_depth("one") == 0
    assert c.side_depth("one", "SELL") == c.bid_depth("one")   # fillable on the bid
    assert c.side_depth("one", "BUY") == 0                     # nothing to buy
    assert c.bid_depth("two") > 0 and c.ask_depth("two") > 0
    assert c.side_depth("missing", "SELL") == 0                # unknown token, no crash


def test_me_bid_only_book_passes_g5_and_records_depth():
    cache = LocalOrderBookCache()
    cache.update("A", [(0.55, 100), (0.54, 100)], [])  # sum of bids 1.05 -> ME breach
    cache.update("B", [(0.50, 100), (0.49, 100)], [])
    mA = MarketInfo(token_id="A", question="Outcome A?", neg_risk=True, neg_risk_market_id="NR1")
    mB = MarketInfo(token_id="B", question="Outcome B?", neg_risk=True, neg_risk_market_id="NR1")
    cluster = Cluster("ME::NR1", [mA, mB], "MUTUAL_EXCLUSIVE", 1.0)
    eng = ArbEngine(cache, cluster_map=None, shadow_logger=None, db=None)
    sts = asyncio.run(eng.evaluate_cluster(cluster))
    me = [s for s in sts if s.violation.violation_type.value == "mutual_exclusive"]
    assert me, "expected an ME violation"
    st = me[0]
    assert st.reason == "PASS", st.reason                       # was G5-rejected on min()=0
    assert st.book_depth_a > CFG.min_book_depth_usd             # real bid-side depth recorded
    assert st.book_depth_b > CFG.min_book_depth_usd


def test_leg_with_empty_sell_side_excluded():
    cache = LocalOrderBookCache()
    cache.update("C", [(0.55, 100)], [])
    cache.update("D", [], [(0.50, 100)])               # D has only asks -> no sellable bid
    mC = MarketInfo(token_id="C", question="Outcome C?", neg_risk=True, neg_risk_market_id="NR2")
    mD = MarketInfo(token_id="D", question="Outcome D?", neg_risk=True, neg_risk_market_id="NR2")
    cl = Cluster("ME::NR2", [mC, mD], "MUTUAL_EXCLUSIVE", 1.0)
    sts = asyncio.run(ArbEngine(cache, None, None, None).evaluate_cluster(cl))
    assert sts == []                                   # D dropped (bid<=0) -> <2 legs -> no phantom


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  [ok] {fn.__name__}")
    print(f"\nALL {len(fns)} SIDE-DEPTH TESTS PASSED")


if __name__ == "__main__":
    main()
