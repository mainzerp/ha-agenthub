"""Session memory service: semantic search over past conversation turns.

Every stored conversation turn is embedded (write path: ``index_turn``,
fired from ``ConversationManager.store_turn``) and matched against incoming
requests (read path: ``search``, run as a prelude overlap task on cache-miss
turns). Matches are conservative, score-annotated SUGGESTIONS injected into
the General Agent's system prompt -- never executed, never treated as facts
(v1.9.6 lesson). Old sessions are only ever READ; their content is copied
into the injected context, never modified or reactivated.

The ``memory.*`` setting keys are seeded elsewhere; every read here falls
back to an inline default so the service works before the seed exists.
Settings are read at call time (hot-reloadable, no caching of memory.*).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.cache.embedding import get_embedding_engine
from app.db.repositories.settings import _settings_float, _settings_int
from app.db.repository import MemoryRepository, SettingsRepository
from app.memory.vector_store import SessionMemoryVectorStore, get_memory_vector_store

logger = logging.getLogger(__name__)

# Inline defaults for the memory.* settings (seeded rows live in the
# settings seed; the service must tolerate them being absent).
_DEFAULT_ENABLED = True
_DEFAULT_SCOPE = "user"
# Default is "blocking": with cached routing the classification finishes in
# ~0ms and even fast providers (~260ms) routinely beat a ~20ms warm embed, so
# best_effort almost never lands a match (live-test finding 2026-08-07).
_DEFAULT_WAIT_MODE = "blocking"
_DEFAULT_WAIT_TIMEOUT_MS = 800
_DEFAULT_SIMILARITY_THRESHOLD = 0.85
_DEFAULT_MAX_MATCHES = 3
_DEFAULT_MAX_SNIPPET_CHARS = 300
_DEFAULT_MAX_CONTINUATION_TURNS = 5

_EMBED_TEXT_MAX_CHARS = 1000
_SUMMARY_MAX_CHARS = 200
_BACKFILL_BATCH_SIZE = 16
_DEFAULT_EMBEDDING_DIM = 768

_VALID_WAIT_MODES = ("best_effort", "blocking")


@dataclass
class MemoryMatch:
    """One matched past session, annotated with its similarity score."""

    conversation_id: str
    session_id: int
    similarity: float
    matched_text: str
    snippet_turns: list[dict[str, Any]] = field(default_factory=list)
    continuation_turns: list[dict[str, Any]] = field(default_factory=list)
    last_turn_at: int | None = None
    user_id: str | None = None


class MemoryService:
    """Search + index + backfill for the session-memory feature."""

    def __init__(self, vector_store: SessionMemoryVectorStore | None = None) -> None:
        # Injected in tests; in production resolved lazily via the singleton
        # so a store-open failure degrades to "memory off" instead of
        # breaking service construction.
        self._vector_store = vector_store

    def _store(self) -> SessionMemoryVectorStore | None:
        if self._vector_store is not None:
            return self._vector_store
        try:
            return get_memory_vector_store()
        except Exception:
            logger.warning("Session-memory vector store unavailable", exc_info=True)
            return None

    async def is_enabled(self) -> bool:
        try:
            raw = await SettingsRepository.get_value("memory.enabled", "true" if _DEFAULT_ENABLED else "false")
        except Exception:
            logger.debug("memory.enabled read failed; using default", exc_info=True)
            return _DEFAULT_ENABLED
        if raw is None:
            return _DEFAULT_ENABLED
        return str(raw).strip().lower() not in ("false", "0", "no", "off")

    async def wait_config(self) -> tuple[str, int]:
        """Return (wait_mode, wait_timeout_ms) for the prelude overlap task."""
        try:
            mode = str(await SettingsRepository.get_value("memory.wait_mode", _DEFAULT_WAIT_MODE) or "")
        except Exception:
            logger.debug("memory.wait_mode read failed; using default", exc_info=True)
            mode = ""
        if mode not in _VALID_WAIT_MODES:
            mode = _DEFAULT_WAIT_MODE
        timeout_ms = await _settings_int("memory.wait_timeout_ms", default=_DEFAULT_WAIT_TIMEOUT_MS)
        return mode, timeout_ms

    @staticmethod
    def _scope_allows(scope: str, row_user_id: str | None, request_user_id: str | None) -> bool:
        """Per-user visibility (D4).

        Global scope sees everything. Per-user scope: an identified user sees
        only their own rows; an anonymous request sees only the anonymous
        bucket (``user_id IS NULL``); the anonymous bucket is never visible
        to an identified user.
        """
        if scope == "global":
            return True
        if request_user_id:
            return bool(row_user_id) and row_user_id == request_user_id
        return not row_user_id

    async def search(
        self,
        query_text: str,
        user_id: str | None,
        current_conversation_id: str | None = None,
    ) -> list[MemoryMatch]:
        """Semantic search over past session turns. Failure-contained: [] on error."""
        try:
            if not query_text or not await self.is_enabled():
                return []
            store = self._store()
            if store is None or not store.vec_available:
                return []
            # Empty-store short-circuit: avoid the embed call entirely.
            if await MemoryRepository.count_turns() == 0:
                return []

            raw_scope = await SettingsRepository.get_value("memory.scope", _DEFAULT_SCOPE)
            scope = str(raw_scope or _DEFAULT_SCOPE).strip().lower()
            if scope not in ("user", "global"):
                scope = _DEFAULT_SCOPE
            threshold = await _settings_float("memory.similarity_threshold", default=_DEFAULT_SIMILARITY_THRESHOLD)
            max_matches = max(1, await _settings_int("memory.max_matches", default=_DEFAULT_MAX_MATCHES))
            max_snippet_chars = await _settings_int("memory.max_snippet_chars", default=_DEFAULT_MAX_SNIPPET_CHARS)
            max_continuation = await _settings_int(
                "memory.max_continuation_turns", default=_DEFAULT_MAX_CONTINUATION_TURNS
            )

            engine = await get_embedding_engine()
            vector = await engine.embed(query_text)
            # Oversample like the routing cache, then post-filter in Python.
            hits = await store.search_async(vector, max(8, max_matches * 3))
            if not hits:
                return []
            rows = await MemoryRepository.get_turns_by_ids([turn_id for turn_id, _ in hits])
            rows_by_id = {int(row["turn_id"]): row for row in rows}

            candidates: list[tuple[float, dict[str, Any]]] = []
            for turn_id, distance in hits:
                row = rows_by_id.get(int(turn_id))
                if row is None:
                    continue
                # The current session's own turns are already in the live
                # conversation context; letting them match would crowd
                # cross-session memory out of the top-k (self-match).
                if current_conversation_id and row.get("conversation_id") == current_conversation_id:
                    continue
                similarity = 1.0 - float(distance)
                if similarity < threshold:
                    continue
                if not self._scope_allows(scope, row.get("user_id"), user_id):
                    continue
                candidates.append((similarity, row))
            if not candidates:
                return []

            # Best match per session, ordered by similarity (recency tiebreak).
            best_by_session: dict[int, tuple[float, dict[str, Any]]] = {}
            for similarity, row in candidates:
                session_id = int(row["session_id"])
                existing = best_by_session.get(session_id)
                if existing is None or similarity > existing[0]:
                    best_by_session[session_id] = (similarity, row)
            ranked = sorted(
                best_by_session.values(),
                key=lambda item: (item[0], item[1].get("last_turn_at") or 0),
                reverse=True,
            )
            # Drop sessions whose best-matched text duplicates a higher-ranked
            # session (e.g. repeated recall phrasings) -- they carry no new
            # content and would crowd distinct matches out of the top-k.
            seen_texts: set[str] = set()
            ordered: list[tuple[float, dict[str, Any]]] = []
            for item in ranked:
                text_key = " ".join(str(item[1].get("user_text") or "").lower().split())
                if text_key and text_key in seen_texts:
                    continue
                seen_texts.add(text_key)
                ordered.append(item)
            ordered = ordered[:max_matches]

            matches: list[MemoryMatch] = []
            for rank, (similarity, row) in enumerate(ordered):
                snippet_user = str(row.get("user_text") or "")[:max_snippet_chars]
                snippet_response = str(row.get("response_text") or "")[:max_snippet_chars]
                continuation_turns: list[dict[str, Any]] = []
                # Continuation copy: only for the TOP match from a DIFFERENT
                # session -- the matched session's recent turns are copied
                # read-only into the injected context.
                if rank == 0 and max_continuation > 0 and row["conversation_id"] != current_conversation_id:
                    session_turns = await MemoryRepository.get_session_turns(row["conversation_id"])
                    continuation_turns = [
                        {
                            "user_text": str(turn.get("user_text") or ""),
                            "response_text": str(turn.get("response_text") or ""),
                        }
                        for turn in session_turns[-max_continuation:]
                    ]
                matches.append(
                    MemoryMatch(
                        conversation_id=str(row["conversation_id"]),
                        session_id=int(row["session_id"]),
                        similarity=round(similarity, 4),
                        matched_text=snippet_user,
                        snippet_turns=[{"user_text": snippet_user, "response_text": snippet_response}],
                        continuation_turns=continuation_turns,
                        last_turn_at=row.get("last_turn_at"),
                        user_id=row.get("user_id"),
                    )
                )
            return matches
        except Exception:
            logger.warning("Session-memory search failed", exc_info=True)
            return []

    async def index_turn(
        self,
        conversation_id: str,
        conversation_row_id: int,
        user_id: str | None,
        user_text: str,
        response_text: str,
        language: str | None,
        source: str | None,
    ) -> None:
        """Embed + persist one stored turn. Fire-and-forget from store_turn.

        Total failure containment: a memory write failure must never surface
        into the request path.
        """
        try:
            if not conversation_id or not conversation_row_id:
                return
            if not await self.is_enabled():
                return
            store = self._store()
            if store is None or not store.vec_available:
                return
            engine = await get_embedding_engine()
            vector = await engine.embed(f"{user_text}\n{response_text}"[:_EMBED_TEXT_MAX_CHARS])
            now = int(time.time())
            session_id = await MemoryRepository.upsert_session(
                conversation_id=conversation_id,
                user_id=user_id,
                summary_text=(user_text or "")[:_SUMMARY_MAX_CHARS],
                language=language,
                source=source,
                turn_epoch=now,
            )
            turn_id = await MemoryRepository.insert_turn_ref(
                session_id=session_id,
                conversation_row_id=conversation_row_id,
                user_id=user_id,
                created_at=now,
            )
            vec_rowid = await store.store_embedding_async(turn_id, vector)
            if vec_rowid is not None:
                info = engine.get_info()
                await MemoryRepository.set_turn_vec(
                    turn_id,
                    vec_rowid,
                    str(info.get("model") or ""),
                    len(vector),
                )
        except Exception:
            logger.warning("Session-memory index_turn failed for %s", conversation_id, exc_info=True)

    async def backfill(self) -> None:
        """Re-embed turns without a vector (post model/dim reset). No-op when clean."""
        try:
            if not await self.is_enabled():
                return
            store = self._store()
            if store is None or not store.vec_available:
                return
            engine = await get_embedding_engine()
            info = engine.get_info()
            model = str(info.get("model") or "")
            dim = int(
                info.get("dimensions") or await _settings_int("embedding.dimension", default=_DEFAULT_EMBEDDING_DIM)
            )
            reset = await asyncio.to_thread(store.ensure_active_model, model, dim)
            if reset:
                cleared = await MemoryRepository.reset_vec_refs()
                logger.info("Session-memory embedding model changed; %d turns queued for re-embed", cleared)
            while True:
                rows = await MemoryRepository.list_unembedded_turns(limit=_BACKFILL_BATCH_SIZE)
                if not rows:
                    return
                for row in rows:
                    text = f"{row.get('user_text') or ''}\n{row.get('response_text') or ''}"[:_EMBED_TEXT_MAX_CHARS]
                    vector = await engine.embed(text)
                    vec_rowid = await store.store_embedding_async(int(row["turn_id"]), vector)
                    if vec_rowid is not None:
                        await MemoryRepository.set_turn_vec(int(row["turn_id"]), vec_rowid, model, len(vector))
                # Yield between batches so the backfill never starves requests.
                await asyncio.sleep(0)
        except Exception:
            logger.warning("Session-memory backfill failed", exc_info=True)


_memory_service: MemoryService | None = None


def get_memory_service() -> MemoryService | None:
    """Return the singleton MemoryService, creating it lazily.

    Failure-contained: returns None when construction fails so callers can
    degrade to "no memory" without touching the request path.
    """
    global _memory_service
    if _memory_service is None:
        try:
            _memory_service = MemoryService()
        except Exception:
            logger.warning("MemoryService initialization failed", exc_info=True)
            return None
    return _memory_service


def init_memory_service() -> MemoryService | None:
    """Initialize the singleton at bootstrap (same lazy semantics as the getter)."""
    return get_memory_service()
