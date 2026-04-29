"""Hybrid entity matching engine."""

from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from app.db.repository import EntityVisibilityRepository, SettingsRepository
from app.entity.aliases import AliasResolver
from app.entity.index import EntityIndex
from app.entity.signals import AliasSignal, EmbeddingSignal, JaroWinklerSignal, LevenshteinSignal, PhoneticSignal
from app.entity.visibility import filter_visible_results
from app.models.entity_index import EntityIndexEntry

logger = logging.getLogger(__name__)


def _normalize_for_containment(text: str) -> str:
    """Normalize text for containment checks: lowercase, strip diacritics, collapse German digraphs."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return text


_DIGRAPH_RE = re.compile(r"(ae|oe|ue)", re.IGNORECASE)


def _digraphs_to_umlauts(text: str) -> str | None:
    """Convert German digraphs to umlauts: ae->a, oe->o, ue->u.
    Returns None if no digraphs are found in the text.
    """
    if not _DIGRAPH_RE.search(text):
        return None
    mapping = {"ae": "\u00e4", "oe": "\u00f6", "ue": "\u00fc", "Ae": "\u00c4", "Oe": "\u00d6", "Ue": "\u00dc"}
    result = text
    for digraph, umlaut in mapping.items():
        result = result.replace(digraph, umlaut)
    return result


@dataclass
class MatchResult:
    """Result of entity matching with per-signal scores."""

    entity_id: str
    friendly_name: str
    score: float
    signal_scores: dict[str, float] = field(default_factory=dict)


class EntityMatcher:
    """Hybrid entity matcher combining fuzzy, alias, and embedding signals.

    Uses all 5 signals (Levenshtein, Jaro-Winkler, Phonetic, Embedding, Alias).
    Weights are loaded from entity_matching_config table.
    """

    def __init__(
        self,
        entity_index: EntityIndex,
        alias_resolver: AliasResolver,
    ) -> None:
        self._entity_index = entity_index
        self._alias_resolver = alias_resolver
        self._weights: dict[str, float] = {}
        self._confidence_threshold: float = 0.60
        self._top_n: int = 3
        self._oversample_factor: int = 20
        # 0.23.0: optional language-agnostic on-demand expansion service.
        # Wired by runtime_setup; leave None in tests that do not need it.
        self._expansion_service = None
        self._index_language: str | None = None
        self._log_misses: bool = True

    async def load_config(self) -> None:
        """Load matching weights and thresholds from DB."""
        from app.db.schema import get_db_read

        async with get_db_read() as db:
            cursor = await db.execute("SELECT key, value FROM entity_matching_config")
            rows = await cursor.fetchall()
            raw_weights = {row[0]: float(row[1]) for row in rows}

        # All 5 active signals
        active_keys = [
            "weight.levenshtein",
            "weight.jaro_winkler",
            "weight.phonetic",
            "weight.embedding",
            "weight.alias",
        ]
        active_raw = {k: raw_weights.get(k, 0.0) for k in active_keys}
        total = sum(active_raw.values())
        if total > 0:
            self._weights = {k.split(".")[-1]: v / total for k, v in active_raw.items()}
        else:
            self._weights = {
                "levenshtein": 0.2,
                "jaro_winkler": 0.2,
                "phonetic": 0.2,
                "embedding": 0.2,
                "alias": 0.2,
            }

        self._confidence_threshold = float(
            await SettingsRepository.get_value("entity_matching.confidence_threshold", "0.60")
        )
        self._top_n = int(await SettingsRepository.get_value("entity_matching.top_n_candidates", "3"))
        try:
            raw_factor = await SettingsRepository.get_value("entity_matching.oversample_factor", "20")
            self._oversample_factor = max(2, min(200, int(raw_factor)))
        except (TypeError, ValueError):
            self._oversample_factor = 20
        try:
            log_misses = await SettingsRepository.get_value("entity_matching.log_misses", "true")
            self._log_misses = (log_misses or "true").lower() in ("1", "true", "yes", "on")
        except Exception:
            self._log_misses = True
        logger.info(
            "Entity matcher config: weights=%s threshold=%s top_n=%s oversample_factor=%s",
            self._weights,
            self._confidence_threshold,
            self._top_n,
            self._oversample_factor,
        )

    async def match(
        self,
        query: str,
        candidates: list[EntityIndexEntry] | None = None,
        agent_id: str | None = None,
        *,
        verbatim_terms: list[str] | None = None,
        preferred_domains: tuple[str, ...] | None = None,
        source_language: str | None = None,
    ) -> list[MatchResult]:
        """Match a query against entities using all active signals.

        Args:
            query: User text (e.g. "kitchen light", "living room lamp").
            candidates: Optional pre-filtered candidates. If None, uses entity_index search.
            agent_id: Optional agent ID for entity visibility filtering.
            verbatim_terms: Optional original-language tokens preserved by
                the orchestrator. Tried verbatim before any embedding-only
                lookup so a translated condensed task ("bedroom") never
                clobbers the user's original word ("Schlafzimmer").
            preferred_domains: Optional ordered tuple of HA domains. Used
                only as a tie-breaker when scores are otherwise equal.
            source_language: Optional ISO language code for the original
                user input; consumed by on-demand expansion fallback.

        Returns:
            Sorted list of MatchResult (highest score first), filtered by confidence threshold.
        """
        expansions_used: list[str] = []
        # 1. Try verbatim terms first.
        if verbatim_terms:
            for term in verbatim_terms:
                if not term:
                    continue
                results = await self._match_query(
                    term,
                    candidates=candidates,
                    agent_id=agent_id,
                    preferred_domains=preferred_domains,
                )
                if results:
                    return results

        # 2. Try the (possibly translated) main query.
        results = await self._match_query(
            query,
            candidates=candidates,
            agent_id=agent_id,
            preferred_domains=preferred_domains,
        )
        if results:
            return results

        # 3. On-demand expansion fallback.
        if self._expansion_service is not None and (verbatim_terms or query):
            tokens_to_expand: list[str] = []
            if verbatim_terms:
                tokens_to_expand.extend(t for t in verbatim_terms if t)
            if query and query not in tokens_to_expand:
                tokens_to_expand.append(query)
            for token in tokens_to_expand:
                try:
                    expansions = await self._expansion_service.expand(
                        token,
                        source_language=source_language,
                        index_language=self._index_language,
                    )
                except Exception:
                    logger.debug("Expansion service raised", exc_info=True)
                    expansions = []
                for exp in expansions:
                    if exp in expansions_used:
                        continue
                    expansions_used.append(exp)
                    exp_results = await self._match_query(
                        exp,
                        candidates=candidates,
                        agent_id=agent_id,
                        preferred_domains=preferred_domains,
                    )
                    if exp_results:
                        return exp_results

        # Miss: emit structured diagnostic.
        if self._log_misses:
            with contextlib.suppress(Exception):
                logger.info(
                    "entity_match_diag query=%r verbatim_terms=%s expansions_used=%s top_candidates=%s",
                    query,
                    verbatim_terms or [],
                    expansions_used,
                    [],
                )
        return []

    async def _match_query(
        self,
        query: str,
        candidates: list[EntityIndexEntry] | None = None,
        agent_id: str | None = None,
        *,
        preferred_domains: tuple[str, ...] | None = None,
    ) -> list[MatchResult]:
        """Inner matcher: scores a single query string against the index."""
        results: dict[str, MatchResult] = {}

        # Embedding shortlist size: oversample when downstream filtering
        # (agent visibility or preferred-domain re-ranking) will prune
        # candidates before the top_n slice.
        filtering_active = bool(agent_id) or bool(preferred_domains)
        embedding_n = (
            max(self._top_n * 2, self._top_n * self._oversample_factor) if filtering_active else self._top_n * 2
        )

        # 1. Alias signal (fast path -- exact match)
        alias_result = await AliasSignal.score(query, self._alias_resolver)
        if alias_result:
            entity_id, alias_score = alias_result
            results[entity_id] = MatchResult(
                entity_id=entity_id,
                friendly_name="",
                score=0.0,
                signal_scores={"alias": alias_score},
            )

        # 2. Embedding signal -- vector search
        try:
            embedding_results = await EmbeddingSignal.score(query, self._entity_index, n=embedding_n)
        except Exception:
            logger.warning("Embedding signal unavailable, proceeding with remaining signals")
            embedding_results = []
        for entity_id, friendly_name, emb_score in embedding_results:
            if entity_id in results:
                results[entity_id].signal_scores["embedding"] = emb_score
                results[entity_id].friendly_name = friendly_name
            else:
                results[entity_id] = MatchResult(
                    entity_id=entity_id,
                    friendly_name=friendly_name,
                    score=0.0,
                    signal_scores={"embedding": emb_score},
                )

        # 2b. Digraph->umlaut dual embedding search
        umlaut_query = _digraphs_to_umlauts(query)
        if umlaut_query:
            try:
                umlaut_results = await EmbeddingSignal.score(umlaut_query, self._entity_index, n=embedding_n)
            except Exception:
                umlaut_results = []
            for entity_id, friendly_name, emb_score in umlaut_results:
                if entity_id in results:
                    existing = results[entity_id].signal_scores.get("embedding", 0.0)
                    if emb_score > existing:
                        results[entity_id].signal_scores["embedding"] = emb_score
                        results[entity_id].friendly_name = friendly_name
                else:
                    results[entity_id] = MatchResult(
                        entity_id=entity_id,
                        friendly_name=friendly_name,
                        score=0.0,
                        signal_scores={"embedding": emb_score},
                    )

        # 3. Levenshtein signal -- compare query against each candidate friendly_name
        for _entity_id, result in results.items():
            if result.friendly_name:
                lev_score = LevenshteinSignal.score(query, result.friendly_name)
                result.signal_scores["levenshtein"] = lev_score

        # 3b. Jaro-Winkler signal
        for _entity_id, result in results.items():
            if result.friendly_name:
                jw_score = JaroWinklerSignal.score(query, result.friendly_name)
                result.signal_scores["jaro_winkler"] = jw_score

        # 3c. Phonetic signal
        for _entity_id, result in results.items():
            if result.friendly_name:
                ph_score = PhoneticSignal.score(query, result.friendly_name)
                result.signal_scores["phonetic"] = ph_score

        # Compute weighted score for each candidate
        query.lower().strip()
        query_containment = _normalize_for_containment(query)

        # Batch-fetch metadata for all candidates to avoid N+1 ChromaDB calls.
        candidate_ids = list(results.keys())
        entry_map = self._entity_index.get_by_ids(candidate_ids)

        for result in results.values():
            weighted_sum = 0.0
            for signal_name, weight in self._weights.items():
                signal_score = result.signal_scores.get(signal_name, 0.0)
                weighted_sum += weight * signal_score
            result.score = weighted_sum

            # Containment bonus: query is a substring of friendly name
            fn_containment = _normalize_for_containment(result.friendly_name or "")
            if query_containment and fn_containment and query_containment in fn_containment:
                result.score = min(1.0, result.score + 0.3)

            # Area bonus: query matches or is contained in normalized area
            # (slug) name OR human-readable area_name OR id_tokens.
            entry = entry_map.get(result.entity_id)
            if entry:
                best_area_bonus = 0.0
                if entry.area:
                    area_containment = _normalize_for_containment(entry.area)
                    if (
                        query_containment
                        and area_containment
                        and (query_containment == area_containment or query_containment in area_containment)
                    ):
                        best_area_bonus = max(best_area_bonus, 0.30)
                if entry.area_name:
                    area_name_containment = _normalize_for_containment(entry.area_name)
                    if (
                        query_containment
                        and area_name_containment
                        and (query_containment == area_name_containment or query_containment in area_name_containment)
                    ):
                        best_area_bonus = max(best_area_bonus, 0.30)
                if best_area_bonus:
                    result.score = min(1.0, result.score + best_area_bonus)

                # Token-overlap bonus across the union of distinctive
                # entity tokens. Language-agnostic: just normalized tokens.
                query_tokens = {t for t in re.split(r"\W+", query.lower()) if t}
                entity_tokens: set[str] = set()
                for src in (
                    entry.friendly_name,
                    entry.area or "",
                    entry.area_name or "",
                    entry.device_name or "",
                ):
                    if src:
                        entity_tokens.update(t for t in re.split(r"\W+", src.lower()) if t)
                entity_tokens.update(t.lower() for t in (entry.id_tokens or []) if t)
                for alias in entry.aliases or []:
                    entity_tokens.update(t for t in re.split(r"\W+", alias.lower()) if t)
                if query_tokens and entity_tokens:
                    matched = query_tokens & entity_tokens
                    if matched:
                        coverage = len(matched) / len(query_tokens)
                        if coverage >= 1.0:
                            result.score = min(1.0, result.score + 0.20)
                        elif coverage >= 0.5:
                            result.score = min(1.0, result.score + 0.10)

        # Filter by confidence and sort
        filtered = [r for r in results.values() if r.score >= self._confidence_threshold]
        if preferred_domains:
            preferred = tuple(d.lower() for d in preferred_domains if d)

            def _sort_key(r: MatchResult) -> tuple:
                domain = r.entity_id.split(".")[0].lower() if "." in r.entity_id else ""
                # Higher score first; preferred domains break ties.
                domain_rank = preferred.index(domain) if domain in preferred else len(preferred)
                return (-r.score, domain_rank)

            filtered.sort(key=_sort_key)
        else:
            filtered.sort(key=lambda r: r.score, reverse=True)

        # Apply entity visibility filtering if agent_id is provided
        if agent_id:
            filtered = await self._apply_visibility_rules(agent_id, filtered)

        top_results = filtered[: self._top_n]

        return top_results

    async def _apply_visibility_rules(
        self,
        agent_id: str,
        results: list[MatchResult],
    ) -> list[MatchResult]:
        """Filter match results by agent entity visibility rules.

        No rules = no filtering (full access). Rule evaluation is shared
        with cached-action replay so both paths keep the same semantics.
        """
        return await filter_visible_results(
            agent_id,
            results,
            self._entity_index,
            repository=EntityVisibilityRepository,
        )

    async def filter_visible_results(
        self,
        agent_id: str | None,
        results: list[MatchResult],
    ) -> list[MatchResult]:
        """Public wrapper for applying visibility rules to precomputed candidates."""
        if not agent_id:
            return results
        return await self._apply_visibility_rules(agent_id, results)
