"""Shared deterministic-first entity resolution helpers.

This module implements the Directive 4 ordered pipeline for executor
entity resolution: exact entity_id, exact friendly_name, exact alias,
then hybrid matching as a fallback.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from app.entity.matcher import MatchResult
from app.entity.visibility import entity_is_visible, filter_visible_results

logger = logging.getLogger(__name__)

_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_NON_WORD_LOOKUP_RE = re.compile(r"[^\w\s\.]")
_WHITESPACE_RE = re.compile(r"\s+")

# Device nouns that may trail a user query (e.g. "Keller light" -> "Keller").
_TRAILING_DEVICE_NOUNS: frozenset[str] = frozenset(
    {
        "bulb",
        "lamp",
        "lampe",
        "lampen",
        "light",
        "lights",
        "licht",
        "lichter",
        "schalter",
        "switch",
        "switches",
    }
)


def _supports_method(obj: Any, method_name: str) -> bool:
    """Return True when an object or its mock spec exposes a callable method."""
    method = getattr(obj, method_name, None)
    if not callable(method):
        return False

    spec_class = getattr(obj, "_spec_class", None)
    if spec_class and hasattr(spec_class, method_name):
        return True
    if hasattr(type(obj), method_name):
        return True
    return method_name in getattr(obj, "__dict__", {})


def _normalize_lookup_text(text: str) -> str:
    """Normalize an entity lookup query for deterministic comparisons."""
    normalized = unicodedata.normalize("NFKD", text.lower().strip())
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = _NON_WORD_LOOKUP_RE.sub(" ", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _strip_trailing_device_noun(query: str) -> str | None:
    """Strip a trailing light/switch noun from a query like 'Keller light'."""
    normalized_query = _normalize_lookup_text(query)
    parts = normalized_query.split()
    if len(parts) <= 1:
        return None
    if parts[-1] not in _TRAILING_DEVICE_NOUNS:
        return None
    stripped = " ".join(parts[:-1]).strip()
    return stripped or None


async def _list_index_entries(
    entity_index: Any,
    domains: set[str] | frozenset[str] | None = None,
) -> list[Any]:
    """Return indexed entities when the index supports deterministic listing."""
    if not entity_index:
        return []
    if _supports_method(entity_index, "list_entries_async"):
        return await entity_index.list_entries_async(domains=domains)
    if _supports_method(entity_index, "list_entries"):
        return entity_index.list_entries(domains=domains)
    return []


async def _filter_visible_entries(
    entries: list[Any],
    entity_index: Any,
    agent_id: str | None,
) -> list[Any]:
    """Apply the shared visibility filter to deterministic candidates."""
    if not entries:
        return []
    if not agent_id:
        return entries

    visible = await filter_visible_results(
        agent_id,
        [MatchResult(entity_id=entry.entity_id, friendly_name=entry.friendly_name, score=1.0) for entry in entries],
        entity_index,
    )
    visible_ids = {result.entity_id for result in visible}
    return [entry for entry in entries if entry.entity_id in visible_ids]


def rerank_matches_by_area(matches: list[Any], preferred_area_id: str | None) -> list[Any]:
    """Reorder hybrid matcher results to prefer the originating area."""
    if not matches or not preferred_area_id or len(matches) < 2:
        return matches
    top = matches[0]
    if (getattr(top, "area", None) or None) == preferred_area_id:
        return matches
    top_score = getattr(top, "score", 0.0) or 0.0
    for idx in range(1, len(matches)):
        candidate = matches[idx]
        if (getattr(candidate, "area", None) or None) != preferred_area_id:
            continue
        cand_score = getattr(candidate, "score", 0.0) or 0.0
        if cand_score >= top_score - 0.05:
            reordered = list(matches)
            reordered[0], reordered[idx] = reordered[idx], reordered[0]
            return reordered
        break
    return matches


def filter_matches_by_domain(
    matches: list[Any],
    allowed_domains: frozenset[str] | set[str],
    *,
    fallback_to_unfiltered: bool = False,
) -> list[Any]:
    """Drop matcher candidates whose entity_id domain is not allowed."""
    if not matches:
        return []
    filtered: list[Any] = []
    for match in matches:
        entity_id = getattr(match, "entity_id", "") or ""
        if "." not in entity_id:
            continue
        if entity_id.split(".", 1)[0] in allowed_domains:
            filtered.append(match)
    if not filtered:
        if fallback_to_unfiltered:
            return list(matches)
        return []
    if len(filtered) != len(matches):
        kept_top = getattr(filtered[0], "entity_id", "")
        logger.debug(
            "filter_matches_by_domain dropped %d/%d candidates for allowed=%s; kept top=%s",
            len(matches) - len(filtered),
            len(matches),
            sorted(allowed_domains),
            kept_top,
        )
    return filtered


def _select_deterministic_candidate(
    entries: list[Any],
    entity_query: str,
    *,
    preferred_area_id: str | None = None,
    preferred_domain: str | None = None,
) -> tuple[Any | None, str | None]:
    """Select a single deterministic candidate or return an ambiguity message."""
    if not entries:
        return None, None

    if preferred_area_id and len(entries) > 1:
        area_filtered = [entry for entry in entries if (entry.area or None) == preferred_area_id]
        if len(area_filtered) == 1:
            return area_filtered[0], None
        if len(area_filtered) > 1:
            entries = area_filtered

    if preferred_domain:
        domain_entries = [entry for entry in entries if getattr(entry, "domain", None) == preferred_domain]
        if len(domain_entries) == 1:
            return domain_entries[0], None

    if len(entries) == 1:
        return entries[0], None

    return None, f"Multiple entities match '{entity_query}'. Please be more specific."


def _build_resolution_result(
    *,
    entity_query: str,
    metadata: dict[str, Any],
    entity_id: str | None = None,
    friendly_name: str | None = None,
    speech: str | None = None,
) -> dict[str, Any]:
    """Build a normalized entity-resolution result payload."""
    return {
        "entity_id": entity_id,
        "friendly_name": friendly_name or entity_query,
        "speech": speech,
        "metadata": metadata,
    }


def _with_visible_entries(result: dict[str, Any], visible_entries: list[Any]) -> dict[str, Any]:
    """Attach cached visible entries for downstream reuse without re-listing the index."""
    result["_visible_entries"] = list(visible_entries) if visible_entries else []
    return result


def _build_exact_terms(entity_query: str, verbatim_terms: list[str] | None) -> list[str]:
    ordered_terms: list[str] = []
    seen: set[str] = set()
    for term in [*(verbatim_terms or []), entity_query]:
        normalized = (term or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered_terms.append(normalized)
    return ordered_terms


async def resolve_entity_deterministic_first(
    entity_query: str,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    *,
    allowed_domains: frozenset[str] | None = None,
    preferred_area_id: str | None = None,
    verbatim_terms: list[str] | None = None,
    preferred_domains: tuple[str, ...] | None = None,
    enable_exact_alias: bool = True,
    enable_strip_device_noun: bool = False,
    enable_area_fallback: bool = False,
    preferred_domain: str | None = None,
) -> dict[str, Any]:
    """Resolve an entity through deterministic stages before hybrid matching.

    Optional extensions (used by the light executor):
      * ``enable_strip_device_noun`` strips trailing nouns like "light" /
        "switch" and retries an exact friendly_name match.
      * ``enable_area_fallback`` matches the query against entity ``area``
        names when no friendly_name matched.
      * ``preferred_domain`` biases ``_select_deterministic_candidate``
        toward a single entry of that domain (e.g. ``"light"``).
    """
    ordered_terms = _build_exact_terms(entity_query, verbatim_terms)
    metadata: dict[str, Any] = {
        "query": entity_query,
        "normalized_query": _normalize_lookup_text(entity_query),
        "match_count": 0,
        "resolution_path": "unresolved",
        "verbatim_terms_tried": ordered_terms,
    }
    visible_entries: list[Any] = []

    if entity_index and _supports_method(entity_index, "get_by_id"):
        for term in ordered_terms:
            entity_id_query = term.lower()
            if not _ENTITY_ID_RE.fullmatch(entity_id_query):
                continue
            exact_entry = await entity_index.get_by_id_async(entity_id_query)
            if not exact_entry:
                continue
            if agent_id and not await entity_is_visible(agent_id, exact_entry.entity_id, entity_index):
                continue
            metadata.update(
                {
                    "match_count": 1,
                    "resolution_path": "exact_entity_id",
                    "top_entity_id": exact_entry.entity_id,
                    "top_friendly_name": exact_entry.friendly_name or exact_entry.entity_id,
                }
            )
            return _with_visible_entries(
                _build_resolution_result(
                    entity_query=entity_query,
                    metadata=metadata,
                    entity_id=exact_entry.entity_id,
                    friendly_name=exact_entry.friendly_name or exact_entry.entity_id,
                ),
                visible_entries,
            )

    visible_entries = await _filter_visible_entries(
        await _list_index_entries(entity_index, domains=allowed_domains),
        entity_index,
        agent_id,
    )
    normalized_terms = {value for value in (_normalize_lookup_text(term) for term in ordered_terms) if value}

    ambiguous_result: dict[str, Any] | None = None

    if visible_entries and normalized_terms:
        exact_name_matches = [
            entry for entry in visible_entries if _normalize_lookup_text(entry.friendly_name or "") in normalized_terms
        ]
        candidate, ambiguity = _select_deterministic_candidate(
            exact_name_matches,
            entity_query,
            preferred_area_id=preferred_area_id,
            preferred_domain=preferred_domain,
        )
        if candidate:
            metadata.update(
                {
                    "match_count": 1,
                    "resolution_path": "exact_friendly_name",
                    "top_entity_id": candidate.entity_id,
                    "top_friendly_name": candidate.friendly_name or candidate.entity_id,
                }
            )
            return _with_visible_entries(
                _build_resolution_result(
                    entity_query=entity_query,
                    metadata=metadata,
                    entity_id=candidate.entity_id,
                    friendly_name=candidate.friendly_name or candidate.entity_id,
                ),
                visible_entries,
            )
        if ambiguity:
            ambiguous_result = {
                "match_count": len(exact_name_matches),
                "resolution_path": "exact_friendly_name_ambiguous",
                "speech": ambiguity,
            }

        if enable_exact_alias:
            alias_matches = []
            for entry in visible_entries:
                aliases = getattr(entry, "aliases", None) or []
                if any(_normalize_lookup_text(alias) in normalized_terms for alias in aliases if alias):
                    alias_matches.append(entry)
            candidate, ambiguity = _select_deterministic_candidate(
                alias_matches,
                entity_query,
                preferred_area_id=preferred_area_id,
                preferred_domain=preferred_domain,
            )
            if candidate:
                metadata.update(
                    {
                        "match_count": 1,
                        "resolution_path": "exact_alias",
                        "top_entity_id": candidate.entity_id,
                        "top_friendly_name": candidate.friendly_name or candidate.entity_id,
                    }
                )
                return _with_visible_entries(
                    _build_resolution_result(
                        entity_query=entity_query,
                        metadata=metadata,
                        entity_id=candidate.entity_id,
                        friendly_name=candidate.friendly_name or candidate.entity_id,
                    ),
                    visible_entries,
                )
            if ambiguity:
                ambiguous_result = {
                    "match_count": len(alias_matches),
                    "resolution_path": "exact_alias_ambiguous",
                    "speech": ambiguity,
                }

    # ------------------------------------------------------------------
    # Extra deterministic fallbacks (extracted from action_executor)
    # ------------------------------------------------------------------
    if visible_entries and normalized_terms and enable_strip_device_noun:
        stripped_query = _strip_trailing_device_noun(entity_query)
        if stripped_query and stripped_query != _normalize_lookup_text(entity_query):
            stripped_matches = [
                entry
                for entry in visible_entries
                if _normalize_lookup_text(entry.friendly_name or "") == stripped_query
            ]
            candidate, ambiguity = _select_deterministic_candidate(
                stripped_matches,
                entity_query,
                preferred_area_id=preferred_area_id,
                preferred_domain=preferred_domain,
            )
            if candidate:
                metadata.update(
                    {
                        "match_count": 1,
                        "resolution_path": "friendly_name_without_device_noun",
                        "normalized_query_without_device_noun": stripped_query,
                        "top_entity_id": candidate.entity_id,
                        "top_friendly_name": candidate.friendly_name or candidate.entity_id,
                    }
                )
                return _with_visible_entries(
                    _build_resolution_result(
                        entity_query=entity_query,
                        metadata=metadata,
                        entity_id=candidate.entity_id,
                        friendly_name=candidate.friendly_name or candidate.entity_id,
                    ),
                    visible_entries,
                )
            if ambiguity:
                metadata.update(
                    {
                        "match_count": len(stripped_matches),
                        "resolution_path": "friendly_name_without_device_noun_ambiguous",
                        "normalized_query_without_device_noun": stripped_query,
                    }
                )
                ambiguous_result = {
                    "match_count": len(stripped_matches),
                    "resolution_path": "friendly_name_without_device_noun_ambiguous",
                    "speech": ambiguity,
                }

    if visible_entries and normalized_terms and enable_area_fallback:
        area_queries = set(normalized_terms)
        stripped_query = _strip_trailing_device_noun(entity_query)
        if stripped_query:
            area_queries.add(stripped_query)
        domain_set = allowed_domains if allowed_domains is not None else frozenset()
        area_matches = [
            entry
            for entry in visible_entries
            if (not domain_set or getattr(entry, "domain", "") in domain_set)
            and _normalize_lookup_text(entry.area or "") in area_queries
        ]
        candidate, ambiguity = _select_deterministic_candidate(
            area_matches,
            entity_query,
            preferred_area_id=preferred_area_id,
            preferred_domain=preferred_domain,
        )
        if candidate:
            metadata.update(
                {
                    "match_count": 1,
                    "resolution_path": "exact_area",
                    "top_entity_id": candidate.entity_id,
                    "top_friendly_name": candidate.friendly_name or candidate.entity_id,
                }
            )
            return _with_visible_entries(
                _build_resolution_result(
                    entity_query=entity_query,
                    metadata=metadata,
                    entity_id=candidate.entity_id,
                    friendly_name=candidate.friendly_name or candidate.entity_id,
                ),
                visible_entries,
            )
        if ambiguity:
            metadata.update(
                {
                    "match_count": len(area_matches),
                    "resolution_path": "exact_area_ambiguous",
                }
            )
            ambiguous_result = {
                "match_count": len(area_matches),
                "resolution_path": "exact_area_ambiguous",
                "speech": ambiguity,
            }

    if entity_matcher:
        matches = await entity_matcher.match(
            entity_query,
            agent_id=agent_id,
            verbatim_terms=verbatim_terms,
            preferred_domains=preferred_domains or (tuple(sorted(allowed_domains)) if allowed_domains else None),
        )
        filtered_matches = (
            filter_matches_by_domain(matches, allowed_domains) if allowed_domains is not None else matches
        )
        if len(filtered_matches) != len(matches):
            metadata["domain_filter_dropped"] = len(matches) - len(filtered_matches)
            if allowed_domains is not None:
                metadata["domain_filter_allowed"] = sorted(allowed_domains)
        metadata.update({"match_count": len(filtered_matches), "resolution_path": "hybrid_matcher"})
        if filtered_matches:
            original_top = filtered_matches[0]
            reranked = rerank_matches_by_area(filtered_matches, preferred_area_id)
            chosen = reranked[0]
            if chosen is not original_top:
                metadata["area_rerank_from"] = original_top.entity_id
                metadata["area_rerank_reason"] = "preferred_area_match"
            metadata["top_entity_id"] = chosen.entity_id
            metadata["top_friendly_name"] = chosen.friendly_name or chosen.entity_id
            metadata["top_score"] = getattr(chosen, "score", 0.0)
            metadata["signal_scores"] = getattr(chosen, "signal_scores", {})
            return _with_visible_entries(
                _build_resolution_result(
                    entity_query=entity_query,
                    metadata=metadata,
                    entity_id=chosen.entity_id,
                    friendly_name=chosen.friendly_name or chosen.entity_id,
                ),
                visible_entries,
            )

    if ambiguous_result:
        metadata.update(
            {
                "match_count": ambiguous_result["match_count"],
                "resolution_path": ambiguous_result["resolution_path"],
            }
        )
        return _with_visible_entries(
            _build_resolution_result(
                entity_query=entity_query,
                metadata=metadata,
                speech=ambiguous_result["speech"],
            ),
            visible_entries,
        )

    metadata["resolution_path"] = "no_match"
    return _with_visible_entries(
        _build_resolution_result(entity_query=entity_query, metadata=metadata),
        visible_entries,
    )
