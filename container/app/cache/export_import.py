"""Export and import helpers for the v4 action and routing cache tiers."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.cache.sqlite_cache_store import (
    COLLECTION_ACTION_CACHE,
    COLLECTION_ROUTING_CACHE,
)
from app.models.cache import ActionCacheEntry, RoutingCacheEntry

if TYPE_CHECKING:
    from app.cache.cache_manager import CacheManager

logger = logging.getLogger(__name__)


SUPPORTED_FORMAT_VERSION: int = 4
SCHEMA_VERSION: int = 4
ALLOWED_TIERS: tuple[str, ...] = ("action", "routing")
EXPORT_PAGE_SIZE: int = 1000
IMPORT_BATCH_SIZE: int = 500
MAX_IMPORT_BYTES: int = 50 * 1024 * 1024  # 50 MiB
_TIER_TO_COLLECTION: dict[str, str] = {
    "routing": COLLECTION_ROUTING_CACHE,
    "action": COLLECTION_ACTION_CACHE,
}


class ImportValidationError(ValueError):
    """Raised when the export envelope is malformed or unsupported."""


@dataclass
class TierImportResult:
    imported: int = 0
    skipped: int = 0
    re_embedded: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportSummary:
    mode: str  # "merge" | "replace"
    format_version: int
    tiers: dict[str, TierImportResult] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def build_export_filename(tiers: list[str], now: datetime) -> str:
    """Return the suggested attachment filename for an export."""

    canonical = [t for t in tiers if t in ALLOWED_TIERS]
    canonical_set = set(canonical)
    tag = "all" if canonical_set == set(ALLOWED_TIERS) else "-".join(canonical) or "empty"
    ts = now.strftime("%Y%m%d%H%M%S")
    return f"agent-assist-cache-{tag}-{ts}.json"


def _export_tier_entries(cache) -> Iterator[dict]:
    """Yield model-shaped cache entries for one collection."""

    offset = 0
    while True:
        page = cache.get_rows(
            include=["documents", "metadatas"],
            limit=EXPORT_PAGE_SIZE,
            offset=offset,
        )
        ids = (page.get("ids") if page else None) or []
        if not ids:
            return
        documents = page.get("documents") or [None] * len(ids)
        metadatas = page.get("metadatas") or [None] * len(ids)
        for document, metadata in zip(documents, metadatas, strict=False):
            entry = cache._deserialize_entry(document or "", metadata or {}, similarity=1.0)
            if entry is not None:
                yield entry.model_dump()
        if len(ids) < EXPORT_PAGE_SIZE:
            return
        offset += EXPORT_PAGE_SIZE


def iter_export_chunks(
    cache_manager: CacheManager,
    tiers: list[str],
    *,
    app_version: str,
) -> Iterator[bytes]:
    """Yield UTF-8 JSON bytes that together form one v4 export envelope."""

    requested = [tier for tier in tiers if tier in ALLOWED_TIERS]
    embedding_model = "unknown"
    try:
        from app.config import settings as _runtime_settings

        embedding_model = (
            getattr(_runtime_settings, "embedding_local_model", None)
            or getattr(_runtime_settings, "embedding_model", None)
            or "unknown"
        )
    except Exception:
        logger.debug("Failed to read embedding model from settings", exc_info=True)
    header = {
        "format_version": SUPPORTED_FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "source_app_version": app_version,
        "embedding_model": embedding_model,
    }
    header_json = json.dumps(header)
    if not header_json.endswith("}"):
        raise ValueError("Expected header_json to end with '}'")
    yield (header_json[:-1] + ',"tiers":{').encode("utf-8")

    cache_by_tier = {
        "action": cache_manager.action_cache,
        "routing": cache_manager._routing_cache,
    }
    for tier_index, tier in enumerate(requested):
        prefix = "," if tier_index else ""
        yield (f'{prefix}"{tier}":[').encode()
        first = True
        for entry in _export_tier_entries(cache_by_tier[tier]):
            sep = "" if first else ","
            first = False
            yield (sep + json.dumps(entry)).encode("utf-8")
        yield b"]"

    yield b"}}"


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def parse_envelope(raw: bytes) -> dict:
    """Parse + validate a v4 action/routing cache export envelope."""

    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportValidationError("payload too large")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ImportValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ImportValidationError("envelope must be a JSON object")

    fmt_version = envelope.get("format_version")
    if not isinstance(fmt_version, int):
        raise ImportValidationError("format_version must be an integer")
    if fmt_version <= 3:
        logger.warning("legacy v<=3 export not supported; cache will rebuild from live traffic")
        raise ImportValidationError("legacy v<=3 export not supported; cache will rebuild from live traffic")
    if fmt_version != SUPPORTED_FORMAT_VERSION:
        raise ImportValidationError(f"unsupported format_version {fmt_version}")

    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise ImportValidationError(f"schema_version must be {SCHEMA_VERSION}")

    tiers_block = envelope.get("tiers")
    if not isinstance(tiers_block, dict) or not tiers_block:
        raise ImportValidationError("tiers block missing or empty")

    if "response" in tiers_block:
        logger.warning("legacy v<=3 export not supported; cache will rebuild from live traffic")
        raise ImportValidationError("legacy v<=3 export not supported; cache will rebuild from live traffic")

    for tier_name, tier_data in tiers_block.items():
        if tier_name not in ALLOWED_TIERS:
            raise ImportValidationError(f"unsupported tier {tier_name!r}")
        entries = tier_data
        if not isinstance(entries, list):
            raise ImportValidationError(f"tier {tier_name!r} entries must be a list")

    return envelope


def _validate_entry(entry: dict, *, tier: str):
    if tier == "action":
        return ActionCacheEntry.model_validate(entry)
    return RoutingCacheEntry.model_validate(entry)


def _apply_tier_import(cache_manager: CacheManager, tier: str, entries: list[dict], *, mode: str) -> TierImportResult:
    result = TierImportResult()
    tier_cache = cache_manager.action_cache if tier == "action" else cache_manager._routing_cache

    if mode == "replace":
        cache_manager.flush(tier)
    validated_entries: list[ActionCacheEntry | RoutingCacheEntry] = []
    for index, raw_entry in enumerate(entries, start=1):
        try:
            validated_entries.append(_validate_entry(raw_entry, tier=tier))
        except Exception as exc:
            result.warnings.append(f"{tier} entry {index}: {exc}")
            result.skipped += 1
    for start in range(0, len(validated_entries), IMPORT_BATCH_SIZE):
        chunk = validated_entries[start : start + IMPORT_BATCH_SIZE]
        ids = [tier_cache.make_entry_id(entry.query_text, language=entry.language) for entry in chunk]
        documents = [entry.query_text for entry in chunk]
        metadatas = [tier_cache._serialize_metadata(entry) for entry in chunk]  # type: ignore[arg-type]
        cache_manager._cache_store.upsert(
            _TIER_TO_COLLECTION[tier],
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=None,
        )
        result.imported += len(chunk)

    tier_cache._enforce_lru()

    return result


async def import_envelope(
    cache_manager: CacheManager,
    envelope: dict,
    *,
    mode: str,
    tiers: list[str],
) -> ImportSummary:
    """Apply a parsed v4 envelope to the cache."""

    import asyncio

    if mode not in ("merge", "replace"):
        raise ImportValidationError(f"unsupported import mode {mode!r}")
    requested = [tier for tier in tiers if tier in ALLOWED_TIERS]
    if not requested:
        raise ImportValidationError("no supported tiers in import request")

    summary = ImportSummary(
        mode=mode,
        format_version=int(envelope.get("format_version", SUPPORTED_FORMAT_VERSION)),
    )
    source_model = envelope.get("embedding_model")
    if source_model and source_model != "unknown":
        try:
            from app.config import settings as _runtime_settings

            local_model = getattr(_runtime_settings, "embedding_local_model", None)
            if local_model and local_model != source_model:
                summary.warnings.append(
                    f"embedding model mismatch: export={source_model!r} runtime={local_model!r} (entries will be re-embedded)"
                )
        except Exception:
            logger.debug("Failed to read embedding model for mismatch check", exc_info=True)
    tiers_block = envelope.get("tiers") or {}

    for tier in requested:
        entries = tiers_block.get(tier)
        if not isinstance(entries, list):
            summary.warnings.append(f"tier {tier!r} not present in envelope")
            summary.tiers[tier] = TierImportResult()
            continue
        tier_result = await asyncio.to_thread(
            _apply_tier_import,
            cache_manager,
            tier,
            entries,
            mode=mode,
        )
        summary.tiers[tier] = tier_result

    return summary
