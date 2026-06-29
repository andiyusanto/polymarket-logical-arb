"""Market clustering: find logical dependency families across markets.

Three sources of clusters, in order of trust:

  1. MUTUALLY_EXCLUSIVE — deterministic. Markets sharing a neg_risk_market_id are
     outcomes of one NegRisk event; their YES prices must sum to <= 1.00. No LLM
     needed (pure accounting identity → confidence 1.0, skips G2/G3).

  2. TEMPORAL / THRESHOLD — LLM pair detection (Anthropic, primary). Batches of
     market questions are sent to the model, which returns DETERMINISTIC pairs
     only (subset relationships, not correlation). Pairs above
     CFG.llm_confidence_threshold are stitched into connected-component clusters.

  3. Embedding fallback — if the Anthropic API is unavailable, sentence-
     transformers cosine + DBSCAN groups near-duplicate questions so the bot
     still surfaces candidate clusters for review (never auto-traded).

build_cluster_map() returns a ClusterMap (a dict cluster_id -> list[MarketInfo]
that also exposes get_cluster_for / all_token_ids for the engine) and writes the
first detected pairs to cluster_review.log for the human spot-check (Rule 4).
"""

import json
import logging
import time
from typing import Optional

from core.config import CFG
from core.models import Cluster, MarketInfo, MarketPair

log = logging.getLogger("arb.clustering")

_LLM_PROMPT = """You are analyzing Polymarket prediction markets to find logical dependencies.

For each pair of markets below that have a DETERMINISTIC logical relationship
(not just correlation), identify:
1. The dependency type: TEMPORAL | THRESHOLD | MUTUAL_EXCLUSIVE
2. Your confidence (0.0-1.0) that both markets will resolve CONSISTENTLY
3. The direction of the constraint (which should be higher/lower/sum)

Return ONLY valid JSON, no preamble:
{{
  "pairs": [
    {{
      "market_a_idx": 0,
      "market_b_idx": 3,
      "dependency_type": "TEMPORAL",
      "confidence": 0.92,
      "constraint": "P(market_a) <= P(market_b) because market_a deadline is earlier",
      "reasoning": "market_a asks about X by Sept, market_b asks about X by Dec. If X happens by Sept, it necessarily happened by Dec."
    }}
  ]
}}

IMPORTANT: Only include pairs where the logical relationship is DETERMINISTIC,
not probabilistic. "Will BTC hit $100k by Sept?" and "Will BTC hit $100k by Dec?"
are deterministic (subset relationship). "Will BTC hit $100k?" and "Will ETH hit
$5k?" are NOT — they are correlated but not logically dependent.

Markets:
{market_list}
"""


class ClusterMap(dict):
    """cluster_id -> list[MarketInfo], plus engine-facing lookups.

    Subclasses dict so callers can iterate .items()/len() (Phase 7 test) while
    the bot uses get_cluster_for() / all_token_ids() (Phase 5 loop).
    """

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, dict] = {}           # cluster_id -> {type, confidence}
        self.pairs: list[MarketPair] = []
        self._token_to_cluster: dict[str, str] = {}

    def _index(self) -> None:
        self._token_to_cluster.clear()
        for cid, markets in self.items():
            for m in markets:
                self._token_to_cluster[m.token_id] = cid

    def get_cluster_for(self, token_id: str) -> Optional[Cluster]:
        cid = self._token_to_cluster.get(token_id)
        if cid is None:
            return None
        meta = self.meta.get(cid, {})
        return Cluster(
            cluster_id=cid,
            markets=self[cid],
            dependency_type=meta.get("dependency_type", "UNKNOWN"),
            llm_confidence=meta.get("llm_confidence", 0.0),
            refreshed_at=meta.get("refreshed_at", 0.0),
        )

    def all_token_ids(self) -> list[str]:
        return [t for cid in self for t in (m.token_id for m in self[cid])]

    def tradeable_clusters(self) -> list[Cluster]:
        """Clusters whose confidence clears the LLM gate (Rule 4)."""
        out = []
        for cid in self:
            meta = self.meta.get(cid, {})
            if meta.get("llm_confidence", 0.0) >= CFG.llm_confidence_threshold:
                out.append(self.get_cluster_for(self[cid][0].token_id))
        return out


class MarketClusterer:
    def __init__(self) -> None:
        self._anthropic = None
        self._anthropic_failed = False

    # ── Public API ───────────────────────────────────────────────────
    async def build_cluster_map(self, markets: list[MarketInfo]) -> ClusterMap:
        cmap = ClusterMap()
        now = time.time()

        # 1) Deterministic NegRisk (mutually-exclusive) clusters.
        me_clusters = self._negrisk_clusters(markets)
        for cid, group in me_clusters.items():
            cmap[cid] = group
            cmap.meta[cid] = {
                "dependency_type": "MUTUAL_EXCLUSIVE",
                "llm_confidence": 1.0,
                "refreshed_at": now,
            }

        # 2) LLM temporal/threshold pairs over the remaining markets.
        remaining = [m for m in markets if m.token_id not in cmap.all_token_ids()]
        pairs = await self._detect_pairs(remaining)
        cmap.pairs = pairs

        # Stitch pairs into connected-component clusters per dependency type.
        comp_clusters = self._components_from_pairs(remaining, pairs)
        for cid, (dep_type, conf, group) in comp_clusters.items():
            cmap[cid] = group
            cmap.meta[cid] = {
                "dependency_type": dep_type,
                "llm_confidence": conf,
                "refreshed_at": now,
            }

        cmap._index()
        self._write_review_log(cmap)
        log.info(
            "Clusters: %d total (%d mutual-exclusive, %d temporal/threshold), "
            "%d LLM pairs",
            len(cmap),
            len(me_clusters),
            len(comp_clusters),
            len(pairs),
        )
        return cmap

    # ── 1) NegRisk grouping ──────────────────────────────────────────
    @staticmethod
    def _negrisk_clusters(markets: list[MarketInfo]) -> dict[str, list[MarketInfo]]:
        groups: dict[str, list[MarketInfo]] = {}
        for m in markets:
            if m.neg_risk and m.neg_risk_market_id:
                groups.setdefault(m.neg_risk_market_id, []).append(m)
        return {
            f"ME::{k}": v for k, v in groups.items() if len(v) >= 2
        }

    # ── 2) LLM pair detection ────────────────────────────────────────
    async def _detect_pairs(self, markets: list[MarketInfo]) -> list[MarketPair]:
        if not markets:
            return []
        client = self._get_anthropic()
        if client is None:
            log.warning(
                "Anthropic unavailable — falling back to embedding clustering"
            )
            return self._embedding_pairs(markets)

        pairs: list[MarketPair] = []
        batch = CFG.llm_pair_batch_size
        for start in range(0, len(markets), batch):
            chunk = markets[start : start + batch]
            try:
                pairs.extend(await self._llm_batch(client, chunk))
            except Exception as exc:
                log.warning("LLM batch %d failed: %s", start, exc)
        return [p for p in pairs if p.llm_confidence >= CFG.llm_confidence_threshold]

    async def _llm_batch(self, client, chunk: list[MarketInfo]) -> list[MarketPair]:
        listing = "\n".join(
            f"[{i}] {m.question}" + (f"  (desc: {m.description[:160]})"
                                     if m.description else "")
            for i, m in enumerate(chunk)
        )
        prompt = _LLM_PROMPT.format(market_list=listing)
        # anthropic SDK exposes a sync client; run it off the event loop.
        import asyncio

        def _call():
            return client.messages.create(
                model=CFG.llm_model,
                max_tokens=CFG.llm_max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )

        resp = await asyncio.get_running_loop().run_in_executor(None, _call)
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        data = self._parse_json(text)
        out: list[MarketPair] = []
        for p in data.get("pairs", []):
            ia, ib = p.get("market_a_idx"), p.get("market_b_idx")
            if ia is None or ib is None or ia == ib:
                continue
            if not (0 <= ia < len(chunk) and 0 <= ib < len(chunk)):
                continue
            dep = str(p.get("dependency_type", "")).upper()
            # ME is NOT taken from the LLM: the sum<=1 arb requires the outcomes
            # to be collectively EXHAUSTIVE, which only a NegRisk event guarantees
            # (handled deterministically in _negrisk_clusters). An LLM "mutually
            # exclusive" label on arbitrary markets can be non-exhaustive → a
            # sum>1 there is not a real arb. Only temporal/threshold come from here.
            if dep not in ("TEMPORAL", "THRESHOLD"):
                continue
            out.append(
                MarketPair(
                    market_a=chunk[ia],
                    market_b=chunk[ib],
                    dependency_type=dep,
                    llm_confidence=float(p.get("confidence", 0.0)),
                    cluster_id="",
                    constraint=str(p.get("constraint", "")),
                    reasoning=str(p.get("reasoning", "")),
                )
            )
        return out

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # salvage the outermost {...}
            i, j = text.find("{"), text.rfind("}")
            if 0 <= i < j:
                try:
                    return json.loads(text[i : j + 1])
                except (json.JSONDecodeError, ValueError):
                    pass
        log.warning("LLM returned unparseable JSON (first 200 chars): %s", text[:200])
        return {"pairs": []}

    # ── Connected components from pairs ───────────────────────────────
    @staticmethod
    def _components_from_pairs(
        markets: list[MarketInfo], pairs: list[MarketPair]
    ) -> dict[str, tuple]:
        by_token = {m.token_id: m for m in markets}
        # Union-find keyed by (dependency_type, token) so different relationship
        # families never merge into one cluster.
        parent: dict[tuple, tuple] = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        conf: dict[tuple, float] = {}
        for p in pairs:
            ta, tb = p.market_a.token_id, p.market_b.token_id
            if ta not in by_token or tb not in by_token:
                continue
            ka, kb = (p.dependency_type, ta), (p.dependency_type, tb)
            union(ka, kb)
            root = find(ka)
            conf[root] = max(conf.get(root, 0.0), p.llm_confidence)

        comps: dict[tuple, list[tuple]] = {}
        for key in list(parent):
            comps.setdefault(find(key), []).append(key)

        clusters: dict[str, tuple] = {}
        idx = 0
        for root, members in comps.items():
            if len(members) < 2:
                continue
            dep_type = root[0]
            group = [by_token[tok] for (_dt, tok) in members if tok in by_token]
            if len(group) < 2:
                continue
            cid = f"{dep_type[:4]}::{idx}"
            clusters[cid] = (dep_type, conf.get(root, 0.0), group)
            idx += 1
        return clusters

    # ── 3) Embedding fallback ─────────────────────────────────────────
    def _embedding_pairs(self, markets: list[MarketInfo]) -> list[MarketPair]:
        try:
            from sentence_transformers import SentenceTransformer
            from sklearn.cluster import DBSCAN
        except ImportError:
            log.warning(
                "sentence-transformers/sklearn not installed — no fallback "
                "clusters. Install them or set ANTHROPIC_API_KEY."
            )
            return []
        model = SentenceTransformer("all-MiniLM-L6-v2")
        embs = model.encode([m.question for m in markets], normalize_embeddings=True)
        labels = DBSCAN(eps=CFG.cluster_eps, min_samples=2, metric="cosine").fit_predict(
            embs
        )
        groups: dict[int, list[MarketInfo]] = {}
        for m, lbl in zip(markets, labels):
            if lbl >= 0:
                groups.setdefault(lbl, []).append(m)
        pairs: list[MarketPair] = []
        for group in groups.values():
            # Pair the first market with the rest; low confidence — review only.
            for other in group[1:]:
                pairs.append(
                    MarketPair(
                        market_a=group[0],
                        market_b=other,
                        dependency_type="TEMPORAL",
                        llm_confidence=0.5,  # below default gate — never auto-traded
                        cluster_id="",
                        reasoning="embedding-fallback (sentence-transformers DBSCAN)",
                    )
                )
        return pairs

    # ── Anthropic client ─────────────────────────────────────────────
    def _get_anthropic(self):
        if self._anthropic is not None or self._anthropic_failed:
            return self._anthropic
        if not CFG.anthropic_api_key:
            self._anthropic_failed = True
            return None
        try:
            import anthropic

            self._anthropic = anthropic.Anthropic(api_key=CFG.anthropic_api_key)
        except Exception as exc:
            log.warning("Anthropic init failed: %s", exc)
            self._anthropic_failed = True
        return self._anthropic

    # ── Review log ───────────────────────────────────────────────────
    @staticmethod
    def _write_review_log(cmap: ClusterMap) -> None:
        try:
            with open("cluster_review.log", "a") as f:
                f.write(f"\n===== cluster build {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                for p in cmap.pairs[:20]:
                    f.write(
                        f"[{p.dependency_type} conf={p.llm_confidence:.2f}]\n"
                        f"  A: {p.market_a.question}\n"
                        f"  B: {p.market_b.question}\n"
                        f"  constraint: {p.constraint}\n"
                        f"  reasoning: {p.reasoning}\n"
                    )
                for cid in cmap:
                    meta = cmap.meta.get(cid, {})
                    if meta.get("dependency_type") == "MUTUAL_EXCLUSIVE":
                        qs = [m.question for m in cmap[cid]]
                        f.write(
                            f"[MUTUAL_EXCLUSIVE {cid}] {len(qs)} outcomes:\n"
                            + "".join(f"  - {q}\n" for q in qs[:8])
                        )
        except OSError as exc:
            log.debug("cluster_review.log write failed: %s", exc)
