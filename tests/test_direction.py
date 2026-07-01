"""Direction classification + monotonicity for threshold and temporal checks.

    python tests/test_direction.py

Covers the bugs fixed in this engine: threshold "dip to" inversion, the
"or lower/higher" suffix (Fed/inflation ladders), temporal persistence/negation
inversion, and the persistence phrasings ("hold above", "through Q4") that the
first regex pass missed. Plain asserts, no pytest dependency.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as M

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.constraints import (  # noqa: E402
    _temporal_direction,
    _threshold_direction,
    check_temporal_monotonicity,
    check_threshold_monotonicity,
)

SEP = datetime(2026, 9, 30, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 31, tzinfo=timezone.utc)


def mk(tok, q, d=None):
    return M(token_id=tok, question=q, end_date=d)


def thr(ms, p):
    return check_threshold_monotonicity(ms, p, cache=None)


def tmp(ms, p):
    return check_temporal_monotonicity(ms, p, cache=None)


def test_threshold_direction_classifier():
    cases = {
        "Will Bitcoin dip to $55,000 by Dec 31?": "down",
        "Bitcoin above $58,000 on July 1?": "up",
        "Will Bitcoin reach $100,000 in June?": "up",
        "Will the Fed's lower bound reach 0.25% or lower before 2027?": "down",
        "Will the Fed's upper bound reach 4.25% or higher before 2027?": "up",
        "Will the Fed's lower bound reach 4% or higher before 2027?": "up",   # trap
        "Will inflation be 5% or more in 2026?": "up",
        "Will unemployment be 5% or less in 2026?": "down",
        "Bitcoin price $60,000 question?": None,                              # fail-closed
        # approach verb ('hit'/'reach') on a bare %-threshold: direction depends
        # on which side the metric starts from → too ambiguous to leg → None
        "Will Trump's approval rating hit 35% in 2026?": None,
        "Will Trump's approval rating reach 30% in 2026?": None,
        "Will Bitcoin hit $100k or higher?": "up",            # explicit rise cue wins
        "Will inflation reach 5% or lower in 2026?": "down",  # explicit fall cue wins
    }
    for q, want in cases.items():
        assert _threshold_direction(q) == want, (q, _threshold_direction(q), want)


def test_threshold_monotonicity():
    m55 = mk("t55", "Will Bitcoin dip to $55,000 by Dec 31?")
    m45 = mk("t45", "Will Bitcoin dip to $45,000 by Dec 31?")
    # down natural ordering (P(55k) > P(45k)) is NOT a violation
    assert thr([m55, m45], {"t55": (0.60, 0.62), "t45": (0.33, 0.35)}) == []
    # down breach: rarer ($45k) priced above likelier ($55k) -> SELL $45k, BUY $55k
    v = thr([m55, m45], {"t55": (0.38, 0.40), "t45": (0.55, 0.57)})
    assert len(v) == 1 and v[0].pair.market_a.token_id == "t45"
    # up regression
    u90 = mk("u90", "Will Bitcoin exceed $90,000 by Dec 31?")
    u100 = mk("u100", "Will Bitcoin exceed $100,000 by Dec 31?")
    assert thr([u90, u100], {"u90": (0.50, 0.52), "u100": (0.30, 0.32)}) == []
    v = thr([u90, u100], {"u90": (0.30, 0.32), "u100": (0.50, 0.52)})
    assert len(v) == 1 and v[0].pair.market_a.token_id == "u100"
    # mixed direction never legged together
    up = mk("mu", "Will Bitcoin exceed $50,000 by Dec 31?")
    dn = mk("md", "Will Bitcoin dip to $50,000 by Dec 31?")
    assert thr([up, dn], {"mu": (0.90, 0.92), "md": (0.10, 0.12)}) == []
    # Fed "or lower" ladder: natural ordering no-arb, breach legs correctly
    f025 = mk("f025", "Will the Fed's lower bound reach 0.25% or lower before 2027?")
    f05 = mk("f05", "Will the Fed's lower bound reach 0.5% or lower before 2027?")
    assert thr([f025, f05], {"f025": (0.20, 0.22), "f05": (0.45, 0.47)}) == []
    v = thr([f025, f05], {"f025": (0.55, 0.57), "f05": (0.38, 0.40)})
    assert len(v) == 1 and v[0].pair.market_a.token_id == "f025"


def test_temporal_direction_classifier():
    cases = {
        "Will X happen by Dec?": "by",
        "Will Bitcoin reach $100,000 by Dec 31?": "by",
        "Will Bitcoin stay below $50,000 until Dec 31?": "until",
        "Will Bitcoin NOT reach $100,000 by Dec 31?": "until",
        "Will Bitcoin remain above $80,000 throughout 2026?": "until",
        "Will the team fail to qualify by Dec?": "until",
        "Will the bill pass through congress by December 2026?": "by",   # collision
        "Will the government hold elections by December 2026?": "by",    # collision
        # persistence phrasings that escaped the first regex pass:
        "Will Bitcoin hold above $80,000 through Q4 2026?": "until",
        "Will Bitcoin keep above $80,000 through 2026?": "until",
        "Will the incumbent retain control through Dec 2026?": "until",
        "Will the company maintain profitability through 2026?": "until",
        "Will X keep the promise by Dec?": "by",                         # bare keep stays by
    }
    for q, want in cases.items():
        assert _temporal_direction(q) == want, (q, _temporal_direction(q), want)


def test_temporal_monotonicity():
    e = mk("e", "Will Bitcoin reach $100,000 by Sept?", SEP)
    l = mk("l", "Will Bitcoin reach $100,000 by Dec?", DEC)
    assert tmp([e, l], {"e": (0.30, 0.32), "l": (0.58, 0.60)}) == []      # by natural
    v = tmp([e, l], {"e": (0.58, 0.60), "l": (0.40, 0.42)})               # by breach
    assert len(v) == 1 and v[0].pair.market_a.token_id == "e"
    e2 = mk("e2", "Will Bitcoin stay below $50,000 until Sept?", SEP)
    l2 = mk("l2", "Will Bitcoin stay below $50,000 until Dec?", DEC)
    assert tmp([e2, l2], {"e2": (0.60, 0.62), "l2": (0.33, 0.35)}) == []  # until natural
    v = tmp([e2, l2], {"e2": (0.38, 0.40), "l2": (0.55, 0.57)})           # until breach
    assert len(v) == 1 and v[0].pair.market_a.token_id == "l2"           # SELL later (rarer)
    assert tmp([e, l2], {"e": (0.9, 0.92), "l2": (0.1, 0.12)}) == []      # mixed not compared


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  [ok] {fn.__name__}")
    print(f"\nALL {len(fns)} DIRECTION TESTS PASSED")


if __name__ == "__main__":
    main()
