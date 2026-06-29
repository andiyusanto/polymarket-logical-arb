"""Two-layer ambiguity detection: structural (rule-based) + semantic (LLM).

No ML classifier yet — Rule 5 forbids it until there are >=200 labelled examples
from RESOLVED markets. The two-layer system is the right tool until then.

  Layer 1 — structural_score(): cheap, deterministic. Flags wording asymmetries
            that break a logical relationship (different resolution source,
            timing qualifier, scope, or data source). Runs on every violation.

  Layer 2 — semantic_score(): an Anthropic call asking whether the two markets
            will DEFINITELY resolve consistently, or whether an edge case could
            split them. Only runs on violations that clear Layer 1, since it
            costs an API round-trip.

A mutually-exclusive violation skips BOTH layers (the math is an accounting
identity within one NegRisk event) — the signal engine handles that bypass.
"""

import logging
import re

from core.config import CFG

log = logging.getLogger("arb.ambiguity")

# Critical wording families. If one question contains a term from a group and the
# other does not, the resolution criteria may differ → reduce the score. Matching
# is WORD-BOUNDARY based (\b) so "low" never matches "below/follow", "high" never
# matches "highlight", "max" never matches "maximum's" neighbours, etc.
_WORDING_GROUPS = [
    # resolution source / authority
    {"official", "officially", "certified", "confirmed"},
    {"projected", "estimated", "forecast", "expected", "preliminary"},
    # timing qualifier
    {"as of", "at the close", "at close", "snapshot"},
    {"by end of", "by the end", "anytime", "ever", "at any point"},
    # scope
    {"including", "incl", "inclusive"},
    {"excluding", "excl", "not including"},
    {"adjusted", "seasonally adjusted"},
    {"unadjusted", "non-adjusted", "not adjusted"},
    # aggregation
    {"average", "mean", "median"},
    {"peak", "high", "maximum", "max"},
    {"minimum", "low", "trough"},
]

_GROUP_PATTERNS = [
    re.compile(r"\b(?:" + "|".join(re.escape(t) for t in sorted(g, key=len,
               reverse=True)) + r")\b", re.IGNORECASE)
    for g in _WORDING_GROUPS
]
_UNIT_PATTERN = re.compile(r"%|\bpercent\b|\bbps\b|\bbasis point", re.IGNORECASE)

_PENALTY_PER_GROUP = 0.18


def structural_score(question_a: str, question_b: str) -> float:
    """Layer 1: 1.0 = no wording asymmetry; lower = potential resolution mismatch.

    Below CFG.structural_score_threshold → the signal engine rejects (G2).
    """
    if not question_a or not question_b:
        return 0.0
    score = 1.0
    for pat in _GROUP_PATTERNS:
        if bool(pat.search(question_a)) != bool(pat.search(question_b)):
            score -= _PENALTY_PER_GROUP

    # Number-format mismatch: one side cites a unit/percentage the other omits
    # (e.g. "%", "bps") often signals different measurement bases.
    if bool(_UNIT_PATTERN.search(question_a)) != bool(_UNIT_PATTERN.search(question_b)):
        score -= _PENALTY_PER_GROUP

    return max(0.0, min(1.0, round(score, 4)))


_SEMANTIC_PROMPT = """You are auditing whether two Polymarket markets will resolve CONSISTENTLY
under a claimed {dependency_type} logical relationship.

Market A:
  Question: {qa}
  Description: {da}

Market B:
  Question: {qb}
  Description: {db}

The claimed relationship implies their probabilities must satisfy a monotonicity
constraint. Your job: decide whether they will DEFINITELY resolve consistently,
or whether there is a realistic edge case where one resolves YES and the other NO
in a way that BREAKS the relationship (different resolution sources, timing
boundaries, rounding, scope, cancellation, or definitional gaps).

Return ONLY valid JSON, no preamble:
{{"consistency": 0.0-1.0, "reasoning": "..."}}

consistency = your probability that the two markets resolve consistently.
1.0 = airtight logical necessity; 0.5 = real ambiguity; 0.0 = they measure
different things and the relationship is illusory.
"""


async def semantic_score(
    question_a: str,
    description_a: str,
    question_b: str,
    description_b: str,
    dependency_type: str,
    _client=None,
) -> float:
    """Layer 2: LLM consistency probability (0.0-1.0).

      >= CFG.semantic_score_threshold (0.85) → HIGH
      >= 0.70                                 → MEDIUM
      <  0.70                                 → reject (LOW)

    Returns 0.0 if the API is unavailable (fail-closed — no trade on unknowns).
    """
    client = _client or _get_anthropic()
    if client is None:
        log.debug("semantic_score: Anthropic unavailable → 0.0 (fail-closed)")
        return 0.0

    prompt = _SEMANTIC_PROMPT.format(
        dependency_type=dependency_type,
        qa=question_a,
        da=(description_a or "")[:600],
        qb=question_b,
        db=(description_b or "")[:600],
    )

    import asyncio

    def _call():
        return client.messages.create(
            model=CFG.llm_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

    try:
        resp = await asyncio.get_running_loop().run_in_executor(None, _call)
    except Exception as exc:
        log.warning("semantic_score LLM call failed: %s", exc)
        return 0.0

    text = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    )
    val = _parse_consistency(text)
    return val


def _parse_consistency(text: str) -> float:
    import json

    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
    try:
        data = json.loads(t)
        return max(0.0, min(1.0, float(data.get("consistency", 0.0))))
    except (json.JSONDecodeError, ValueError, TypeError):
        m = re.search(r'consistency"?\s*[:=]\s*([01]?\.?\d+)', text)
        if m:
            try:
                return max(0.0, min(1.0, float(m.group(1))))
            except ValueError:
                pass
    log.warning("semantic_score: unparseable LLM response: %s", text[:160])
    return 0.0


_anthropic = None
_anthropic_failed = False


def _get_anthropic():
    global _anthropic, _anthropic_failed
    if _anthropic is not None or _anthropic_failed:
        return _anthropic
    if not CFG.anthropic_api_key:
        _anthropic_failed = True
        return None
    try:
        import anthropic

        _anthropic = anthropic.Anthropic(api_key=CFG.anthropic_api_key)
    except Exception as exc:
        log.warning("Anthropic init failed: %s", exc)
        _anthropic_failed = True
    return _anthropic
