"""Shared deterministic-first entity resolution helpers.

This module implements the Directive 4 ordered pipeline for executor
entity resolution: exact entity_id, exact friendly_name, exact alias,
then hybrid matching as a fallback.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.entity.aliases import AliasResolver
from app.entity.tokens import fold_text
from app.entity.visibility import entity_is_visible, filter_visible_results

logger = logging.getLogger(__name__)

_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_NON_WORD_LOOKUP_RE = re.compile(r"[^\w\s.]|_")
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


def _serialize_match_candidates(matches: list[Any], entity_index: Any | None = None) -> list[dict[str, Any]]:
    """Serialize a list of MatchResult candidates for metadata/logging."""
    candidates: list[dict[str, Any]] = []
    for match in matches:
        entity_id = getattr(match, "entity_id", "") or ""
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        entry = None
        if entity_index is not None and hasattr(entity_index, "get_by_id"):
            try:
                entry = entity_index.get_by_id(entity_id)
            except Exception:
                entry = None
        candidates.append(
            {
                "entity_id": entity_id,
                "friendly_name": getattr(match, "friendly_name", "") or entity_id,
                "domain": domain,
                "area": getattr(entry, "area", None) if entry else None,
                "score": round(float(getattr(match, "score", 0.0) or 0.0), 4),
                "signal_scores": {
                    k: round(float(v), 4) for k, v in (getattr(match, "signal_scores", {}) or {}).items()
                },
            }
        )
    return candidates


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
    """Normalize an entity lookup query for deterministic comparisons.

    Applies the shared :func:`app.entity.tokens.fold_text` folding
    (lowercase, NFKD, strip combining marks, ``ß`` -> ``ss``, German
    digraphs ae/oe/ue collapsed) so the exact stages fold exactly like
    the hybrid matcher. Then replaces non-word characters AND
    underscores with spaces (``_`` is a ``\\w`` char but acts as a word
    separator in user-typed snake_case queries like "jalousie_mitte")
    and collapses whitespace.
    """
    normalized = _NON_WORD_LOOKUP_RE.sub(" ", fold_text(text))
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _name_contains_term(norm_name: str, term: str) -> bool:
    """Word-boundary containment of a normalized term in a normalized name."""
    return norm_name.startswith(term + " ") or norm_name.endswith(" " + term) or f" {term} " in norm_name


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
        entries,
        entity_index,
    )
    return visible


_AREA_RERANK_MARGIN = 0.05
# Hybrid matcher ambiguity margin: when the runner-up scores within this
# distance of the top candidate (and neither the speaker's area nor the
# caller's preferred domain separates them), the resolver asks instead of
# silently picking one of two near-equal entities.
_HYBRID_AMBIGUITY_MARGIN = 0.02


def _match_area(match: Any, entity_index: Any | None) -> str | None:
    """Return a match's area id: its own ``area`` attribute, else the index entry's.

    ``MatchResult`` carries no area, so the index is the source of truth;
    a candidate object that does carry ``area`` (index entries, test
    doubles) is used directly.
    """
    area = getattr(match, "area", None)
    if isinstance(area, str) and area:
        return area
    if entity_index is None or not _supports_method(entity_index, "get_by_id"):
        return None
    try:
        entry = entity_index.get_by_id(getattr(match, "entity_id", "") or "")
    except Exception:
        return None
    area = getattr(entry, "area", None) if entry is not None else None
    return area if isinstance(area, str) and area else None


def rerank_matches_by_area(
    matches: list[Any],
    preferred_area_id: str | None,
    entity_index: Any | None = None,
) -> list[Any]:
    """Reorder hybrid matcher results to prefer the originating area.

    Areas are read from the entity index (``MatchResult`` has no area
    field); a candidate in ``preferred_area_id`` scoring within
    ``_AREA_RERANK_MARGIN`` of the top candidate is moved to the front.
    """
    if not matches or not preferred_area_id or len(matches) < 2:
        return matches
    top = matches[0]
    if _match_area(top, entity_index) == preferred_area_id:
        return matches
    top_score = getattr(top, "score", 0.0) or 0.0
    for idx in range(1, len(matches)):
        candidate = matches[idx]
        if _match_area(candidate, entity_index) != preferred_area_id:
            continue
        cand_score = getattr(candidate, "score", 0.0) or 0.0
        if cand_score >= top_score - _AREA_RERANK_MARGIN:
            reordered = list(matches)
            reordered[0], reordered[idx] = reordered[idx], reordered[0]
            return reordered
        break
    return matches


def _numeric_score(match: Any) -> float | None:
    """Return a candidate's score as float, or None when it carries no numeric score."""
    score = getattr(match, "score", None)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def _break_hybrid_tie(
    matches: list[Any],
    entity_index: Any | None,
    *,
    preferred_area_id: str | None = None,
    preferred_domain: str | None = None,
) -> Any | None:
    """Return the hybrid top candidate, or None when it is ambiguous.

    Candidates scoring within ``_HYBRID_AMBIGUITY_MARGIN`` of the top are
    near-ties. A near-tie is broken only by the same signals the
    deterministic stages use: a single candidate in the speaker's area,
    then a single candidate of the caller's preferred domain. Otherwise
    the resolver fails closed and asks for clarification.
    """
    top = matches[0]
    top_score = _numeric_score(top)
    if top_score is None:
        return top
    near = [
        m for m in matches if (score := _numeric_score(m)) is not None and score >= top_score - _HYBRID_AMBIGUITY_MARGIN
    ]
    if len(near) <= 1:
        return top
    if preferred_area_id:
        in_area = [m for m in near if _match_area(m, entity_index) == preferred_area_id]
        if len(in_area) == 1:
            return in_area[0]
    if preferred_domain:
        in_domain = [m for m in near if (getattr(m, "entity_id", "") or "").split(".", 1)[0] == preferred_domain]
        if len(in_domain) == 1:
            return in_domain[0]
    return None


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


def _with_visible_entries(result: dict[str, Any], visible_entries: list[Any] | None) -> dict[str, Any]:
    """Attach cached visible entries for downstream reuse without re-listing the index.

    The key is absent when resolution finished before the index listing
    ran (lazy exact-``entity_id`` path) -- callers must treat a missing
    key as "no snapshot available", not "no visible entities".
    """
    if visible_entries is not None:
        result["_visible_entries"] = list(visible_entries)
    return result


def _build_exact_terms(entity_query: str) -> list[str]:
    normalized = (entity_query or "").strip()
    return [normalized] if normalized else []


def _normalized_entry_fields(entry: Any) -> tuple[Any, str, list[str], frozenset[str]]:
    """Compute an entry's normalized match fields once per snapshot.

    The last element holds the normalized area id slug AND the
    human-readable ``area_name`` so the area fallback matches what users
    actually say ("Wohnzimmer") as well as the slug ("wohnzimmer").
    Both are guarded by isinstance checks: entries without real string
    areas (e.g. lightweight test doubles) must stay tolerated.
    """
    areas: set[str] = set()
    for raw_area in (getattr(entry, "area", None), getattr(entry, "area_name", None)):
        if isinstance(raw_area, str) and raw_area:
            normalized_area = _normalize_lookup_text(raw_area)
            if normalized_area:
                areas.add(normalized_area)
    return (
        entry,
        _normalize_lookup_text(entry.friendly_name or ""),
        [_normalize_lookup_text(alias) for alias in (getattr(entry, "aliases", None) or []) if alias],
        frozenset(areas),
    )


async def _user_alias_entity_ids(entity_matcher: Any, normalized_terms: set[str]) -> set[str]:
    """Entity ids whose user/DB alias (``aliases`` table) equals a normalized term.

    The alias table is reached through the matcher's ``AliasResolver``
    (loaded from the DB, including YAML user aliases). Only a real
    ``AliasResolver`` is consulted so mocked matchers stay inert. The
    caller intersects the result with the visibility- and
    domain-filtered snapshot (Directive 5).
    """
    alias_resolver = getattr(entity_matcher, "alias_resolver", None) if entity_matcher is not None else None
    if not isinstance(alias_resolver, AliasResolver) or not normalized_terms:
        return set()
    try:
        alias_map = await alias_resolver.list_all()
    except Exception:
        logger.debug("User alias lookup failed; skipping DB alias stage", exc_info=True)
        return set()
    return {
        entity_id
        for alias, entity_id in alias_map.items()
        if entity_id and _normalize_lookup_text(alias) in normalized_terms
    }


async def resolve_entity_deterministic_first(
    entity_query: str,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    *,
    allowed_domains: frozenset[str] | None = None,
    preferred_area_id: str | None = None,
    preferred_domains: tuple[str, ...] | None = None,
    enable_exact_alias: bool = True,
    enable_strip_device_noun: bool = False,
    enable_area_fallback: bool = False,
    preferred_domain: str | None = None,
    visible_entries: list[Any] | None = None,
) -> dict[str, Any]:
    """Resolve an entity through deterministic stages before hybrid matching.

    The exact ``entity_id`` stage runs before the full index listing
    (lazy listing); a caller may pass a pre-computed, visibility-filtered
    ``visible_entries`` snapshot from the SAME request to skip the
    listing entirely (Directive 5: the snapshot is always computed fresh
    per request by the caller or by this function).

    Optional extensions (used by the light executor):
      * ``enable_strip_device_noun`` strips trailing nouns like "light" /
        "switch" and retries an exact friendly_name match.
      * ``enable_area_fallback`` matches the query against entity ``area``
        names when no friendly_name matched.
      * ``preferred_domain`` biases ``_select_deterministic_candidate``
        toward a single entry of that domain (e.g. ``"light"``).
    """
    ordered_terms = _build_exact_terms(entity_query)
    metadata: dict[str, Any] = {
        "query": entity_query,
        "normalized_query": _normalize_lookup_text(entity_query),
        "match_count": 0,
        "resolution_path": "unresolved",
    }

    # Lazy listing (P2): the exact entity_id stage runs BEFORE the full
    # index listing + visibility filter it never uses. The listing is
    # computed on demand below, only when a later stage needs it (or is
    # supplied by the caller via ``visible_entries``). Directive 5 is
    # preserved: the exact-id path keeps its own visibility check.
    if entity_index and _supports_method(entity_index, "get_by_id"):
        for term in ordered_terms:
            entity_id_query = term.lower()
            if not _ENTITY_ID_RE.fullmatch(entity_id_query):
                continue
            # The executor's allowed domains bound this stage exactly like
            # the listing-based stages below: an out-of-domain entity_id
            # is never selected here.
            if allowed_domains is not None and entity_id_query.split(".", 1)[0] not in allowed_domains:
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

    if visible_entries is None:
        visible_entries = await _filter_visible_entries(
            await _list_index_entries(entity_index, domains=allowed_domains),
            entity_index,
            agent_id,
        )

    normalized_terms = {value for value in (_normalize_lookup_text(term) for term in ordered_terms) if value}
    # Space-insensitive variants: a compound term like "innenhofuberdachung"
    # must match the friendly_name "Innenhof Überdachung" at the exact stage.
    squashed_terms = {value.replace(" ", "") for value in normalized_terms}

    # Normalized match fields are computed ONCE per visible-entries
    # snapshot and reused across the friendly_name / alias /
    # strip-device-noun / area stages below (previously up to four full
    # normalization sweeps per resolution call).
    normalized_entries: list[tuple[Any, str, list[str], str]] = []
    if visible_entries and normalized_terms:
        normalized_entries = [_normalized_entry_fields(entry) for entry in visible_entries]

    ambiguous_result: dict[str, Any] | None = None

    if normalized_entries:
        exact_name_matches = [
            entry
            for entry, norm_name, _, _ in normalized_entries
            if norm_name in normalized_terms or norm_name.replace(" ", "") in squashed_terms
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
            # HA per-entity aliases (on the index entry) and user/DB aliases
            # (``aliases`` table, incl. the YAML user file) are both exact
            # deterministic alias matches. DB aliases only resolve to
            # entities present in the visibility/domain-filtered snapshot.
            user_alias_ids = await _user_alias_entity_ids(entity_matcher, normalized_terms)
            alias_matches = [
                entry
                for entry, _, norm_aliases, _ in normalized_entries
                if entry.entity_id in user_alias_ids
                or any(norm_alias in normalized_terms for norm_alias in norm_aliases)
            ]
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
    if normalized_entries and enable_strip_device_noun:
        stripped_query = _strip_trailing_device_noun(entity_query)
        if stripped_query and stripped_query != _normalize_lookup_text(entity_query):
            stripped_matches = [entry for entry, norm_name, _, _ in normalized_entries if norm_name == stripped_query]
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

    if normalized_entries and enable_area_fallback:
        area_queries = set(normalized_terms)
        stripped_query = _strip_trailing_device_noun(entity_query)
        if stripped_query:
            area_queries.add(stripped_query)
        domain_set = allowed_domains if allowed_domains is not None else frozenset()
        area_matches = [
            entry
            for entry, _, _, norm_areas in normalized_entries
            if (not domain_set or getattr(entry, "domain", "") in domain_set)
            and not norm_areas.isdisjoint(area_queries)
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

    # Word-boundary containment stage (deterministic, embedding-free): the
    # full normalized query appearing as a token-bounded substring of a
    # friendly_name ("front door" inside "Front Door Lock") is a strong
    # deterministic signal -- the removed embedding fallback used to cover
    # this partial-name class. A space-insensitive variant covers compounds
    # ("innenhofuberdachung" inside "innenhof uberdachung mitte"). Runs only
    # when no earlier stage produced an ambiguity; disambiguation mirrors
    # the exact stages (preferred area/domain, otherwise fail-closed).
    if normalized_entries and ambiguous_result is None:
        containment_matches = [
            entry
            for entry, norm_name, _, _ in normalized_entries
            if any(
                _name_contains_term(norm_name, term)
                or (len(term) >= 4 and term.replace(" ", "") in norm_name.replace(" ", ""))
                for term in normalized_terms
            )
        ]
        candidate, ambiguity = _select_deterministic_candidate(
            containment_matches,
            entity_query,
            preferred_area_id=preferred_area_id,
            preferred_domain=preferred_domain,
        )
        if candidate:
            metadata.update(
                {
                    "match_count": 1,
                    "resolution_path": "friendly_name_containment",
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
                "match_count": len(containment_matches),
                "resolution_path": "friendly_name_containment_ambiguous",
                "speech": ambiguity,
            }

    # An ambiguous exact / alias / area / containment stage is a
    # deterministic finding: the hybrid matcher must not override it with a
    # fuzzy pick (Directive 4.1). Ask the user instead.
    if ambiguous_result is None and entity_matcher:
        matches = await entity_matcher.match(
            entity_query,
            agent_id=agent_id,
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
            reranked = rerank_matches_by_area(filtered_matches, preferred_area_id, entity_index)
            chosen = reranked[0]
            if chosen is not original_top:
                metadata["area_rerank_from"] = original_top.entity_id
                metadata["area_rerank_reason"] = "preferred_area_match"
            else:
                chosen = _break_hybrid_tie(
                    filtered_matches,
                    entity_index,
                    preferred_area_id=preferred_area_id,
                    preferred_domain=preferred_domain,
                )
                if chosen is None:
                    metadata.update(
                        {
                            "resolution_path": "hybrid_matcher_ambiguous",
                            "candidate_entities": _serialize_match_candidates(filtered_matches, entity_index),
                        }
                    )
                    return _with_visible_entries(
                        _build_resolution_result(
                            entity_query=entity_query,
                            metadata=metadata,
                            speech=f"Multiple entities match '{entity_query}'. Please be more specific.",
                        ),
                        visible_entries,
                    )
            metadata["top_entity_id"] = chosen.entity_id
            metadata["top_friendly_name"] = chosen.friendly_name or chosen.entity_id
            metadata["top_score"] = getattr(chosen, "score", 0.0)
            metadata["signal_scores"] = getattr(chosen, "signal_scores", {})
            metadata["candidate_entities"] = _serialize_match_candidates(filtered_matches, entity_index)
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
