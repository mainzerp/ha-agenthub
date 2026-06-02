"""Language-agnostic on-demand query expansion service.

Sits between the matcher and the LLM. When a query token has no
candidate above threshold, the matcher asks this service for
equivalents. Results are cached in the ``query_synonym_cache`` table
so a given token in a given language costs at most one extra
LLM round-trip per deployment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.agents.base import _load_prompt_path
from app.db.repository import QuerySynonymCacheRepository, SettingsRepository
from app.security.sanitization import wrap_user_input

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "query_expansion.txt"
_TOKEN_NORMALIZE_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def load_query_expansion_prompt_template(prompt_path: Path | None = None) -> str | None:
    try:
        return _load_prompt_path(prompt_path or _PROMPT_PATH)
    except Exception:
        logger.warning("Failed to read query_expansion prompt", exc_info=True)
        return None


async def load_query_expansion_prompt_template_async(prompt_path: Path | None = None) -> str | None:
    return await asyncio.to_thread(load_query_expansion_prompt_template, prompt_path)


def _normalize_token(token: str) -> str:
    if not token:
        return ""
    text = token.strip().lower()
    text = _TOKEN_NORMALIZE_RE.sub(" ", text)
    return " ".join(text.split())


def _validate_expansions(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or len(cleaned) > 40:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= 8:
            break
    return out


class QueryExpansionService:
    """Expand a query token into language-agnostic synonyms via LLM, with cache."""

    def __init__(
        self,
        *,
        cache_repo: type[QuerySynonymCacheRepository] = QuerySynonymCacheRepository,
        llm_call=None,
        prompt_path: Path | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self._cache = cache_repo
        self._llm_call = llm_call
        self._prompt_path = prompt_path or _PROMPT_PATH
        self._prompt_template = prompt_template
        self._inflight: dict[tuple[str, str], asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()

    async def _get_inflight_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        async with self._lock_guard:
            lock = self._inflight.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._inflight[key] = lock
            return lock

    async def expand(
        self,
        token: str,
        *,
        source_language: str | None,
        index_language: str | None,
    ) -> list[str]:
        norm = _normalize_token(token)
        if not norm:
            return []
        lang_key = (source_language or "").strip().lower()
        try:
            enabled = (await SettingsRepository.get_value("entity_matching.expansion.enabled", "true")) or "true"
        except Exception:
            enabled = "true"
        if enabled.lower() not in ("1", "true", "yes", "on"):
            return []

        cached = await self._cache.get(norm, lang_key)
        if cached is not None:
            try:
                await self._cache.touch(norm, lang_key)
            except Exception:
                logger.debug("cache touch failed", exc_info=True)
            return cached

        key = (norm, lang_key)
        lock = await self._get_inflight_lock(key)
        async with lock:
            try:
                cached = await self._cache.get(norm, lang_key)
                if cached is not None:
                    return cached

                expansions = await self._call_llm(norm, lang_key, (index_language or "").strip().lower())
                try:
                    await self._cache.put(norm, lang_key, expansions)
                except Exception:
                    logger.debug("cache put failed", exc_info=True)
                try:
                    ttl_raw = await SettingsRepository.get_value("entity_matching.expansion.ttl_seconds", "2592000")
                    ttl = int(ttl_raw or "2592000")
                    if ttl > 0:
                        await self._cache.purge_expired(ttl)
                except Exception:
                    logger.debug("Failed to purge expired cache entries", exc_info=True)
                try:
                    cap_raw = await SettingsRepository.get_value("entity_matching.expansion.max_cache_rows", "5000")
                    cap = int(cap_raw or "5000")
                    if cap > 0:
                        await self._cache.evict_lru(cap)
                except Exception:
                    logger.debug("Failed to evict LRU cache entries", exc_info=True)
                return expansions
            finally:
                async with self._lock_guard:
                    self._inflight.pop(key, None)

    async def _get_prompt_template(self) -> str | None:
        if self._prompt_template is not None:
            return self._prompt_template
        self._prompt_template = await load_query_expansion_prompt_template_async(self._prompt_path)
        return self._prompt_template

    async def _call_llm(self, token: str, source_language: str, index_language: str) -> list[str]:
        if self._llm_call is None:
            return []
        template = await self._get_prompt_template()
        if template is None:
            return []
        prompt_token = wrap_user_input(token)
        prompt = (
            template.replace("{token}", prompt_token)
            .replace("{source_language}", source_language or "unknown")
            .replace("{index_language}", index_language or "unknown")
        )
        try:
            raw = await self._llm_call(prompt)
        except Exception:
            logger.debug("LLM expansion call failed", exc_info=True)
            return []
        if not raw:
            return []
        text = raw.strip()
        # Strip markdown fences if present.
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        try:
            data = json.loads(text)
        except Exception:
            return []
        return _validate_expansions(data.get("expansions") if isinstance(data, dict) else None)
