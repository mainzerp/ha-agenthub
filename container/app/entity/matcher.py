"""Hybrid entity matching engine."""

from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.db.repository import EntityVisibilityRepository, SettingsRepository

if TYPE_CHECKING:
    from app.entity.expansion import QueryExpansionService
from app.entity.aliases import AliasResolver
from app.entity.index import EntityIndex
from app.entity.signals import AliasSignal, EmbeddingSignal, JaroWinklerSignal, LevenshteinSignal, PhoneticSignal
from app.entity.tokens import normalize_tokenize
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


_MAX_SPAN_TOKENS = 4
# Floor-Regel: when a single query span covers ALL friendly-name tokens of a
# candidate with similarity >= _FLOOR_MIN_SIMILARITY (the entity name stands
# verbatim in the query), the final score is floored at _FLOOR_SCORE. This
# replaces the deleted reverse-containment bonus; it never lowers a score.
_FLOOR_SCORE = 0.65
_FLOOR_MIN_SIMILARITY = 0.95

# Near-miss token coverage: an entity token counts as covered by a span
# when a span token is a close Levenshtein match (German plural/singular,
# e.g. "jalousien" vs "jalousie"). Length-guarded so short tokens never
# conflate. Only affects coverage/Floor-Regel, never the postings index.
_COVERAGE_NEAR_MISS_MIN_SIMILARITY = 0.85
_COVERAGE_NEAR_MISS_MIN_TOKEN_LEN = 4


def _query_spans(tokens: list[str], max_len: int = _MAX_SPAN_TOKENS) -> list[tuple[str, frozenset[str]]]:
    """Enumerate unique contiguous n-gram spans (1..max_len) of normalized query tokens.

    Returns (span_text, span_token_set) pairs; span_text is the space-joined
    token sequence used for string similarity, the set is used for coverage.
    """
    spans: dict[tuple[str, frozenset[str]], None] = {}
    for n in range(1, min(max_len, len(tokens)) + 1):
        for i in range(len(tokens) - n + 1):
            chunk = tokens[i : i + n]
            spans[(" ".join(chunk), frozenset(chunk))] = None
    return list(spans)


def _idf_coverage(entity_tokens: set[str], span_tokens: frozenset[str], idf_map: dict[str, float]) -> float:
    """IDF-weighted fraction of entity tokens covered by the span's tokens.

    Tokens absent from ``idf_map`` (no postings) are skipped -- no df is
    invented for them. When no entity token carries an IDF weight at all
    (all unseen), falls back to plain unweighted coverage.
    """
    if not entity_tokens:
        return 0.0
    total = sum(idf_map[t] for t in entity_tokens if t in idf_map)
    if total > 0.0:
        matched = sum(idf_map[t] for t in entity_tokens & span_tokens if t in idf_map)
        return matched / total
    return len(entity_tokens & span_tokens) / len(entity_tokens)


def _best_phonetic_token_match(query_tokens: list[str], entity_tokens: set[str]) -> float:
    """Best phonetic score across all query-token/entity-token pairs."""
    best = 0.0
    for q_token in query_tokens:
        for e_token in entity_tokens:
            score = PhoneticSignal.score(q_token, e_token)
            if score > best:
                best = score
                if best >= 1.0:
                    return best
    return best


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
        # 0.23.0: optional language-agnostic on-demand expansion service.
        # Wired by runtime_setup; leave None in tests that do not need it.
        self._expansion_service: QueryExpansionService | None = None
        self._index_language: str | None = None
        self._log_misses: bool = True
        # Token-based candidate preselection. Defaults live here (not only
        # in load_config) because test fixtures bypass load_config.
        self._token_preselection_enabled: bool = True
        self._token_preselection_max_df_ratio: float = 0.5
        self._token_preselection_max_candidates: int = 20

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

        conf_raw = await SettingsRepository.get_value("entity_matching.confidence_threshold", "0.60")
        self._confidence_threshold = float(conf_raw or "0.60")
        top_n_raw = await SettingsRepository.get_value("entity_matching.top_n_candidates", "3")
        self._top_n = int(top_n_raw or "3")
        try:
            log_misses = await SettingsRepository.get_value("entity_matching.log_misses", "true")
            self._log_misses = (log_misses or "true").lower() in ("1", "true", "yes", "on")
        except Exception:
            self._log_misses = True
        try:
            preselection_enabled = await SettingsRepository.get_value(
                "entity_matching.token_preselection.enabled", "true"
            )
            self._token_preselection_enabled = (preselection_enabled or "true").lower() in ("1", "true", "yes", "on")
            df_ratio_raw = await SettingsRepository.get_value("entity_matching.token_preselection.max_df_ratio", "0.5")
            self._token_preselection_max_df_ratio = float(df_ratio_raw or "0.5")
            max_candidates_raw = await SettingsRepository.get_value(
                "entity_matching.token_preselection.max_candidates", "20"
            )
            self._token_preselection_max_candidates = int(max_candidates_raw or "20")
        except Exception:
            self._token_preselection_enabled = True
            self._token_preselection_max_df_ratio = 0.5
            self._token_preselection_max_candidates = 20
        logger.info(
            "Entity matcher config: weights=%s threshold=%s top_n=%s",
            self._weights,
            self._confidence_threshold,
            self._top_n,
        )

    async def match(
        self,
        query: str,
        candidates: list[EntityIndexEntry] | None = None,
        agent_id: str | None = None,
        *,
        preferred_domains: tuple[str, ...] | None = None,
        source_language: str | None = None,
        top_n: int | None = None,
    ) -> list[MatchResult]:
        """Match a query against entities using all active signals.

        Args:
            query: User text (e.g. "kitchen light", "living room lamp").
            candidates: Optional pre-filtered candidates. If None, uses entity_index search.
            agent_id: Optional agent ID for entity visibility filtering.
            preferred_domains: Optional ordered tuple of HA domains. Used
                only as a tie-breaker when scores are otherwise equal.
            source_language: Optional ISO language code for the original
                user input; consumed by on-demand expansion fallback.
            top_n: Optional override for the configured result-count cap.
                The orchestrator's ingress resolution uses this to request an
                oversampled unfiltered pool (agent_id=None) that is later
                re-filtered per routed agent and cut to the envelope's K.

        Returns:
            Sorted list of MatchResult (highest score first), filtered by confidence threshold.
        """
        expansions_used: list[str] = []
        # 1. Match the query directly.
        results = await self._match_query(
            query,
            candidates=candidates,
            agent_id=agent_id,
            preferred_domains=preferred_domains,
            top_n=top_n,
        )
        if results:
            return results

        # 2. On-demand expansion fallback.
        if self._expansion_service is not None and query:
            try:
                expansions = await self._expansion_service.expand(
                    query,
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
                    top_n=top_n,
                )
                if exp_results:
                    return exp_results

        # Miss: emit structured diagnostic.
        if self._log_misses:
            with contextlib.suppress(Exception):
                logger.info(
                    "entity_match_diag query=%r expansions_used=%s top_candidates=%s",
                    query,
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
        top_n: int | None = None,
    ) -> list[MatchResult]:
        """Inner matcher: scores a single query string against the index."""
        results: dict[str, MatchResult] = {}
        effective_top_n = top_n if top_n is not None and top_n > 0 else self._top_n

        # Embedding shortlist size: oversample up to a fixed cap when
        # downstream filtering (agent visibility or preferred-domain
        # re-ranking) will prune candidates before the top_n slice.
        # The dead ``entity_matching.oversample_factor`` setting was
        # removed (L6): for top_n >= 10 the min() clamp made any factor a
        # no-op, and with the shipped default factor the shortlist always
        # landed on this cap -- so the cap is the documented fixed
        # behavior and the effective shortlist is unchanged.
        filtering_active = bool(agent_id) or bool(preferred_domains)
        embedding_n = max(20, effective_top_n * 2) if filtering_active else effective_top_n * 2

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

        # 2. Embedding signal -- vector search (skipped when candidates provided)
        if candidates:
            for entry in candidates:
                results[entry.entity_id] = MatchResult(
                    entity_id=entry.entity_id,
                    friendly_name=entry.friendly_name or "",
                    score=0.0,
                    signal_scores={},
                )
            embedding_results = []
            umlaut_results = []
        else:
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

            # 2c. Token-based preselection: rescue entities whose index tokens
            # match individual query tokens even when the embedding shortlist
            # missed them. Inserted before the visibility filter (Directive 5)
            # so rescued candidates are filtered like any other candidate.
            if self._token_preselection_enabled:
                preselection_tokens = normalize_tokenize(query)
                if preselection_tokens:
                    try:
                        rescued_entries = await self._entity_index.find_by_tokens_async(
                            preselection_tokens,
                            max_df_ratio=self._token_preselection_max_df_ratio,
                            max_candidates=self._token_preselection_max_candidates,
                        )
                    except Exception:
                        logger.debug("Token preselection unavailable, proceeding without it", exc_info=True)
                        rescued_entries = []
                    for entry in rescued_entries:
                        if entry.entity_id in results:
                            # Already shortlisted via alias or embedding: still
                            # set the marker so diagnostics see the token hit
                            # uniformly. The marker is DIAGNOSTICS ONLY -- it
                            # has no scoring effect (the marker-gated
                            # reverse-containment bonus was removed with the
                            # span-scoring redesign; the Floor-Regel took its
                            # place).
                            results[entry.entity_id].signal_scores["token_preselection"] = 1.0
                        else:
                            results[entry.entity_id] = MatchResult(
                                entity_id=entry.entity_id,
                                friendly_name=entry.friendly_name or "",
                                score=0.0,
                                signal_scores={"token_preselection": 1.0},
                            )

        # Apply entity visibility filtering before any scoring so hidden
        # entities are never scored or returned.
        if agent_id:
            results = {r.entity_id: r for r in await self._apply_visibility_rules(agent_id, list(results.values()))}

        # 3. Span-based string signals. Score each candidate against its best
        # normalized query n-gram span (1..4 tokens) instead of the whole
        # query string: full-query scoring systematically deflated on
        # sentence-length queries, which the deleted additive bonuses
        # (containment, reverse-containment, token-overlap) only compensated
        # for. Spans are enumerated once per query.
        query = query.lower().strip()
        query_containment = _normalize_for_containment(query)
        query_tokens = [t for t in re.split(r"[\W_]+", query_containment) if t]
        spans = _query_spans(query_tokens)

        # Batch-fetch metadata for all candidates to avoid N+1 ChromaDB calls.
        candidate_ids = list(results.keys())
        if hasattr(self._entity_index, "get_by_ids_async"):
            entry_map = await self._entity_index.get_by_ids_async(candidate_ids)
        else:
            entry_map = self._entity_index.get_by_ids(candidate_ids)

        # IDF weights for span coverage: one locked read for all candidates.
        entity_tokens_by_id: dict[str, set[str]] = {
            r.entity_id: normalize_tokenize(r.friendly_name) for r in results.values() if r.friendly_name
        }
        all_entity_tokens: set[str] = set()
        for toks in entity_tokens_by_id.values():
            all_entity_tokens.update(toks)
        try:
            idf_map = self._entity_index.token_idf(all_entity_tokens) if all_entity_tokens else {}
        except Exception:
            logger.debug("token_idf unavailable, falling back to unweighted coverage", exc_info=True)
            idf_map = {}

        floored: set[str] = set()
        for result in results.values():
            entity_tokens = entity_tokens_by_id.get(result.entity_id)
            if not entity_tokens:
                continue
            fn = result.friendly_name
            fn_norm = _normalize_for_containment(fn)
            best_lev = 0.0
            best_jw = 0.0
            for span_text, span_tokens in spans:
                if not span_tokens & entity_tokens:
                    continue
                # Near-miss coverage: an entity token also counts as covered
                # when a span token is a close Levenshtein match (e.g. German
                # plural "jalousien" vs singular "jalousie"). Computed once
                # per (candidate, span); only affects coverage/Floor-Regel.
                covered = span_tokens | {
                    et
                    for et in entity_tokens - span_tokens
                    if len(et) >= _COVERAGE_NEAR_MISS_MIN_TOKEN_LEN
                    and any(
                        len(st) >= _COVERAGE_NEAR_MISS_MIN_TOKEN_LEN
                        and LevenshteinSignal.score(et, st) >= _COVERAGE_NEAR_MISS_MIN_SIMILARITY
                        for st in span_tokens
                    )
                }
                cov = _idf_coverage(entity_tokens, covered, idf_map)
                lev = LevenshteinSignal.score(span_text, fn)
                jw = JaroWinklerSignal.score(span_text, fn)
                best_lev = max(best_lev, lev * cov)
                best_jw = max(best_jw, jw * cov)
                # Floor-Regel detection: the entity name stands verbatim in
                # the query (one span covers all friendly-name tokens with
                # similarity >= 0.95 on normalized strings, so umlaut and
                # digraph spellings count as the same name). Near-miss
                # coverage lets plural spellings ("Jalousien Mitte" vs
                # "Jalousie mitte") reach the floor too.
                if entity_tokens <= covered and LevenshteinSignal.score(span_text, fn_norm) >= _FLOOR_MIN_SIMILARITY:
                    floored.add(result.entity_id)
            result.signal_scores["levenshtein"] = best_lev
            result.signal_scores["jaro_winkler"] = best_jw

            # 3c. Phonetic signal -- per-token best match.
            result.signal_scores["phonetic"] = _best_phonetic_token_match(query_tokens, entity_tokens)

        # Compute weighted score for each candidate
        for result in results.values():
            weighted_sum = 0.0
            for signal_name, weight in self._weights.items():
                signal_score = result.signal_scores.get(signal_name, 0.0)
                weighted_sum += weight * signal_score
            result.score = weighted_sum

            # Area bonus: query matches or is contained in normalized area
            # (slug) name OR human-readable area_name OR id_tokens.
            idx_entry: EntityIndexEntry | None = entry_map.get(result.entity_id)
            if idx_entry:
                best_area_bonus = 0.0
                if idx_entry.area:
                    area_containment = _normalize_for_containment(idx_entry.area)
                    if (
                        query_containment
                        and area_containment
                        and (query_containment == area_containment or query_containment in area_containment)
                    ):
                        best_area_bonus = max(best_area_bonus, 0.30)
                if idx_entry.area_name:
                    area_name_containment = _normalize_for_containment(idx_entry.area_name)
                    if (
                        query_containment
                        and area_name_containment
                        and (query_containment == area_name_containment or query_containment in area_name_containment)
                    ):
                        best_area_bonus = max(best_area_bonus, 0.30)
                if best_area_bonus:
                    result.score = min(1.0, result.score + best_area_bonus)

            # Floor-Regel: applied after the weighted sum and kept bonuses,
            # capped at 1.0, and never lowers a score already above the floor.
            if result.entity_id in floored:
                result.score = min(1.0, max(result.score, _FLOOR_SCORE))

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

        top_results = filtered[:effective_top_n]

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
