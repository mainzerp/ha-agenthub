"""Cache management admin API endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.cache.export_import import (
    ALLOWED_TIERS,
    MAX_IMPORT_BYTES,
    ImportValidationError,
    build_export_filename,
    import_envelope,
    iter_export_chunks,
    parse_envelope,
)
from app.cache.vector_store import (
    COLLECTION_ACTION_CACHE,
    COLLECTION_ROUTING_CACHE,
)
from app.config import settings
from app.runtime_setup import ensure_setup_runtime_initialized
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/cache",
    tags=["admin-cache"],
    dependencies=[Depends(require_admin_session)],
)


class FlushRequest(BaseModel):
    tier: str | None = None  # "routing", "action", or None for all


@router.get("/stats")
async def get_cache_stats(request: Request):
    """Cache stats per tier."""
    await ensure_setup_runtime_initialized(request.app)
    cache_manager = request.app.state.cache_manager
    if not cache_manager:
        return {"routing": {}, "action": {}, "status": "not_initialized"}
    try:
        stats = cache_manager.get_stats()
        return stats
    except Exception as exc:
        logger.warning("Failed to get cache stats", exc_info=True)
        return {"error": str(exc)}


@router.get("/entries")
async def browse_cache_entries(
    request: Request,
    tier: str = Query("routing", pattern="^(routing|action|response)$"),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    """Browse/search cache entries by tier."""
    tier = tier or "routing"
    cache_manager = request.app.state.cache_manager
    if not cache_manager:
        return {"entries": [], "total": 0}

    vector_store = cache_manager._vector_store
    collection_name = COLLECTION_ROUTING_CACHE if tier == "routing" else COLLECTION_ACTION_CACHE

    try:
        total = vector_store.count(collection_name)
        if total == 0:
            return {"entries": [], "total": 0, "page": page, "per_page": per_page}

        # When not searching, use limit/offset to avoid loading all entries
        if not search:
            offset_val = (page - 1) * per_page
            data = vector_store.get(
                collection_name,
                include=["metadatas", "documents"],
                limit=per_page,
                offset=offset_val,
            )
            entries = []
            for i, doc_id in enumerate(data["ids"]):
                meta = data["metadatas"][i]
                document = data["documents"][i] if data.get("documents") else ""
                entry = {"id": doc_id, "document": document, **meta}
                entries.append(entry)

            return {
                "entries": entries,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page if per_page else 0,
            }

        # Search requires loading all entries for text filtering
        data = vector_store.get(
            collection_name,
            include=["metadatas", "documents"],
        )
        entries = []
        for i, doc_id in enumerate(data["ids"]):
            meta = data["metadatas"][i]
            document = data["documents"][i] if data.get("documents") else ""
            entry = {"id": doc_id, "document": document, **meta}
            entries.append(entry)

        # Filter by search text
        if search:
            search_lower = search.lower()
            entries = [
                e
                for e in entries
                if search_lower in (e.get("document") or "").lower()
                or search_lower in str(e.get("agent_id", "")).lower()
            ]

        # Sort by last_accessed descending
        entries.sort(key=lambda e: e.get("last_accessed", ""), reverse=True)

        # Paginate
        filtered_total = len(entries)
        offset = (page - 1) * per_page
        entries = entries[offset : offset + per_page]

        return {
            "entries": entries,
            "total": filtered_total,
            "page": page,
            "per_page": per_page,
            "pages": (filtered_total + per_page - 1) // per_page if per_page else 0,
        }
    except Exception as exc:
        logger.warning("Failed to browse cache entries", exc_info=True)
        return {"entries": [], "total": 0, "error": str(exc)}


@router.post("/flush")
async def flush_cache(request: Request, payload: FlushRequest):
    """Flush cache tier(s)."""
    cache_manager = request.app.state.cache_manager
    if not cache_manager:
        return {"status": "error", "detail": "Cache not initialized"}

    tier = payload.tier
    if tier and tier not in ("routing", "action"):
        return {
            "status": "error",
            "detail": "Invalid tier. Use 'routing', 'action', or omit for all.",
        }

    try:
        cache_manager.flush(tier)
        return {"status": "ok", "flushed": tier or "all"}
    except Exception as exc:
        logger.warning("Failed to flush action cache", exc_info=True)
        return {"status": "error", "detail": str(exc)}


@router.get("/export")
async def export_cache(
    request: Request,
    tier: str = Query("all", pattern="^(routing|action|all)$"),
):
    """Stream a JSON envelope containing the requested cache tier(s)."""
    await ensure_setup_runtime_initialized(request.app)
    cache_manager = getattr(request.app.state, "cache_manager", None)
    if cache_manager is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": "cache not initialized"},
        )

    tiers = list(ALLOWED_TIERS) if tier == "all" else [tier]
    raw_version = getattr(settings, "app_version", None)
    app_version = raw_version if isinstance(raw_version, str) else "unknown"
    filename = build_export_filename(tiers, datetime.now(UTC))

    try:
        generator = iter_export_chunks(cache_manager, tiers, app_version=app_version)
    except Exception as exc:
        logger.warning("Failed to start cache export", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": str(exc)},
        )

    return StreamingResponse(
        generator,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_cache(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("merge"),
    tiers: str = Form("routing,action"),
    re_embed: bool = Form(False),
):
    """Import a cache export envelope. Returns an ImportSummary as JSON."""
    cache_manager = getattr(request.app.state, "cache_manager", None)
    if cache_manager is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": "cache not initialized"},
        )

    if mode not in ("merge", "replace"):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": f"invalid mode {mode!r}"},
        )

    requested_tiers = [t.strip() for t in (tiers or "").split(",") if t.strip()]
    requested_tiers = [t for t in requested_tiers if t in ("routing", "action")]
    if not requested_tiers:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "no supported tiers requested"},
        )

    try:
        raw = await file.read()
    except Exception as exc:
        logger.warning("Failed to read cache import upload", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": f"failed to read upload: {exc}"},
        )

    if len(raw) > MAX_IMPORT_BYTES:
        return JSONResponse(
            status_code=413,
            content={"status": "error", "detail": "payload too large"},
        )

    try:
        envelope = parse_envelope(raw)
    except ImportValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": str(exc)},
        )

    try:
        summary = await import_envelope(
            cache_manager,
            envelope,
            mode=mode,
            tiers=requested_tiers,
        )
    except ImportValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": str(exc)},
        )
    except Exception as exc:
        logger.warning("Cache import failed", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": str(exc)},
        )

    return {
        "status": "ok",
        "mode": summary.mode,
        "format_version": summary.format_version,
        "tiers": {
            name: {
                "imported": result.imported,
                "skipped": result.skipped,
                "re_embedded": result.re_embedded,
                "warnings": result.warnings,
            }
            for name, result in summary.tiers.items()
        },
        "warnings": summary.warnings,
    }
