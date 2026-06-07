"""Lightweight span collection for request tracing.

All operations are fire-and-forget: errors are logged, never raised.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from app.db.repository import TraceSpanRepository, TraceSummaryRepository

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "token",
    "api_key",
    "apikey",
    "password",
    "secret",
    "cookie",
    "set-cookie",
    "bearer",
    "credential",
)

_NORMALIZED_SENSITIVE_KEY_MARKERS = tuple(marker.replace("_", "-") for marker in _SENSITIVE_KEY_MARKERS)
_EXACT_SENSITIVE_KEYS = {"code", "key"}

_SAFE_METADATA_KEYS = {
    "action",
    "agent_id",
    "area",
    "area_id",
    "area_name",
    "cached_agent_id",
    "chars",
    "content_agent",
    "count",
    "delivery_type",
    "device_id",
    "device_name",
    "domain",
    "entity",
    "entity_id",
    "error_type",
    "from_agent",
    "hit_type",
    "language",
    "length",
    "model",
    "server_name",
    "service",
    "span_id",
    "status",
    "success",
    "target",
    "target_agent",
    "tps",
    "to_agent",
    "tool_name",
    "ttft_ms",
}

_SENSITIVE_QUERY_MARKERS = (*_SENSITIVE_KEY_MARKERS, "code", "key", "auth")
_NORMALIZED_SENSITIVE_QUERY_MARKERS = tuple(marker.replace("_", "-") for marker in _SENSITIVE_QUERY_MARKERS)

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}\b")
_COMMON_API_KEY_RE = re.compile(
    r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gsk_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z\-_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
_LONG_TOKEN_RE = re.compile(r"\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9_-]{48,})\b")
_JSON_CODE_RE = re.compile(r'(?i)("code"\s*:\s*)(?:"[^"]*"|\d+)')
_CONTEXTUAL_CODE_RE = re.compile(
    r"(?i)\b((?:otp|pin|verification\s+code|one[- ]time\s+code|auth(?:orization)?\s+code|code))\b([^A-Za-z0-9]{0,4})(?!\[?REDACTED)([A-Za-z0-9]{4,8})\b"
)
_CONTEXTUAL_SECRET_RE = re.compile(
    r"(?i)\b(authorization|token|api[_-]?key|apikey|password|secret|cookie|set-cookie|credential)\b(\s*[:=]?\s*)([A-Za-z0-9._~+/=-]{4,})"
)
_MAX_FALLBACK_CHARS = 2000


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("_", "-")


def _is_safe_metadata_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return (
        normalized in _SAFE_METADATA_KEYS
        or normalized.endswith("-id")
        or normalized.endswith("-count")
        or normalized.endswith("-chars")
        or normalized.endswith("-length")
    )


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    if normalized in _EXACT_SENSITIVE_KEYS:
        return True
    if _is_safe_metadata_key(normalized):
        return False
    return any(marker in normalized for marker in _NORMALIZED_SENSITIVE_KEY_MARKERS)


def _is_sensitive_query_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized in _EXACT_SENSITIVE_KEYS or any(
        marker in normalized for marker in _NORMALIZED_SENSITIVE_QUERY_MARKERS
    )


def _encode_query_pairs(pairs: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for key, value in pairs:
        encoded_key = quote(str(key), safe="")
        if value == "":
            parts.append(encoded_key)
        else:
            encoded_value = quote(str(value), safe="[]:/@")
            parts.append(f"{encoded_key}={encoded_value}")
    return "&".join(parts)


def _redacted_placeholder(_value: Any) -> str:
    return "[REDACTED]"


def _redacted_placeholder_for_key(key: str | None, value: Any) -> str:
    normalized = _normalize_key(key) if key is not None else ""
    if normalized == "code":
        return "[REDACTED_CODE]"
    return _redacted_placeholder(value)


def _replace_contextual_secret(match: re.Match[str]) -> str:
    label = match.group(1)
    separator = match.group(2)
    normalized = _normalize_key(label)
    placeholder = (
        "[REDACTED_TOKEN]"
        if any(marker in normalized for marker in ("authorization", "token", "api-key", "apikey"))
        else "[REDACTED]"
    )
    return f"{label}{separator}{placeholder}"


def _maybe_parse_json_string(value: str) -> Any | None:
    candidate = value.strip()
    if not candidate or candidate[0] not in "[{":
        return None
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _sanitize_plain_string(value: str) -> str:
    sanitized = _BEARER_TOKEN_RE.sub("Bearer [REDACTED_TOKEN]", value)
    sanitized = _COMMON_API_KEY_RE.sub("[REDACTED_TOKEN]", sanitized)
    sanitized = _LONG_TOKEN_RE.sub("[REDACTED_TOKEN]", sanitized)
    sanitized = _JSON_CODE_RE.sub(r'\1"[REDACTED_CODE]"', sanitized)
    sanitized = _CONTEXTUAL_CODE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED_CODE]", sanitized)
    sanitized = _CONTEXTUAL_SECRET_RE.sub(_replace_contextual_secret, sanitized)
    return sanitized


def _sanitize_url(url: str) -> str:
    try:
        split = urlsplit(url)
    except Exception:
        return _sanitize_plain_string(url)

    host = split.hostname or ""
    if not host and split.netloc:
        host = split.netloc.rsplit("@", 1)[-1]
    netloc = host
    if split.port:
        netloc = f"{netloc}:{split.port}"

    query_pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        if _is_sensitive_query_key(key):
            query_pairs.append((key, "[REDACTED]"))
        else:
            query_pairs.append((key, _sanitize_plain_string(value)))

    query = _encode_query_pairs(query_pairs)
    fragment = _sanitize_plain_string(split.fragment) if split.fragment else ""
    sanitized = urlunsplit((split.scheme, netloc, split.path, query, fragment))
    return sanitized or _sanitize_plain_string(url)


def _sanitize_string(value: str) -> str:
    parsed = _maybe_parse_json_string(value)
    if parsed is not None:
        return json.dumps(sanitize_trace_value(parsed), ensure_ascii=False)

    sanitized = _URL_RE.sub(lambda match: _sanitize_url(match.group(0)), value)
    return _sanitize_plain_string(sanitized)


def sanitize_trace_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact sensitive trace payloads while preserving safe metadata."""
    if key is not None and _is_sensitive_key(key):
        return _redacted_placeholder_for_key(key, value)

    if isinstance(value, dict):
        return {item_key: sanitize_trace_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_trace_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_trace_value(item) for item in value)
    if isinstance(value, (str, bytes, bytearray)):
        text = value.decode(errors="replace") if isinstance(value, (bytes, bytearray)) else value
        return _sanitize_string(text)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_string(str(value)[:_MAX_FALLBACK_CHARS])


def sanitize_trace_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    sanitized = sanitize_trace_value(metadata)
    return sanitized if isinstance(sanitized, dict) else {"value": sanitized}


# Parent-span tracking per async context (Q-8). A ContextVar is the
# correct mechanism for nested spans under ``asyncio.gather`` so parallel
# branches don't see each other's parents via a shared list.
_current_parent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_parent",
    default=None,
)


SpanSource = Literal["ha", "chat", "api"]


class SpanCollector:
    """Collects spans during a single request and flushes them in bulk.

    FLOW-MED-9: ``source`` must be supplied at construction so it
    cannot silently default to ``"api"`` when a future call site
    forgets to assign it post-hoc. Callers that truly do not know
    the source (middleware building a collector before the route
    handler runs) pass ``source="api"`` explicitly and the route
    handler may rebuild the collector with the correct source if
    needed.
    """

    def __init__(self, trace_id: str, source: SpanSource = "api") -> None:
        self.trace_id = trace_id
        self.source: SpanSource = source
        self._spans: list[dict[str, Any]] = []

    def push_parent(self, span_id: str) -> contextvars.Token:
        """Set ``span_id`` as the current parent for subsequent spans and
        return a token that must be passed to :meth:`pop_parent` when done.

        Used by entry points (middleware / stream handlers) that want every
        nested span to be re-parented under a shared root span.
        """
        return _current_parent.set(span_id)

    def pop_parent(self, token: contextvars.Token) -> None:
        """Restore the previous parent span set by :meth:`push_parent`."""
        try:
            _current_parent.reset(token)
        except ValueError:
            # Token from a different context (e.g. reset after the setting
            # task has finished). Safe to ignore -- parent tracking is
            # best-effort.
            logger.debug("Could not reset _current_parent token", exc_info=True)

    @asynccontextmanager
    async def start_span(
        self,
        name: str,
        agent_id: str | None = None,
        parent_span: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Context manager that records a span's start time and duration."""
        span_id = uuid.uuid4().hex[:12]
        if parent_span is None:
            parent_span = _current_parent.get()

        span: dict[str, Any] = {
            "span_id": span_id,
            "trace_id": self.trace_id,
            "span_name": name,
            "agent_id": agent_id,
            "parent_span": parent_span,
            "start_time": datetime.now(UTC).isoformat(),
            "status": "ok",
            "metadata": {},
        }
        token = _current_parent.set(span_id)
        t0 = time.perf_counter()
        try:
            yield span
        except Exception:
            span["status"] = "error"
            raise
        finally:
            _current_parent.reset(token)
            # Allow callers to override duration (e.g. filler spans with pre-recorded timestamps)
            if "_override_duration_ms" in span:
                span["duration_ms"] = span.pop("_override_duration_ms")
                # Compute end_time from start_time + overridden duration
                try:
                    st = datetime.fromisoformat(span["start_time"])
                    span["end_time"] = (st + timedelta(milliseconds=span["duration_ms"])).isoformat()
                except Exception:
                    logger.debug("Failed to compute end_time from overridden duration", exc_info=True)
                    span["end_time"] = datetime.now(UTC).isoformat()
            else:
                span["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                span["end_time"] = datetime.now(UTC).isoformat()
            self._spans.append(span)

    def get_spans(self) -> list[dict[str, Any]]:
        """Return a shallow copy of the collected spans."""
        return list(self._spans)

    def record_root_span(self, span_data: dict[str, Any]) -> None:
        """Append a pre-built root span (e.g. from middleware timing)."""
        self._spans.append(span_data)

    def add_root_span(self, span_data: dict[str, Any]) -> None:
        """Append a pre-built root span (encapsulated access to _spans)."""
        self._spans.append(span_data)

    async def flush(self) -> None:
        """Bulk insert all collected spans. Fire-and-forget."""
        if not self._spans:
            return
        try:
            for span in self._spans:
                span["metadata"] = sanitize_trace_metadata(span.get("metadata"))
            await TraceSpanRepository.insert_batch(self._spans)
            # Compute and store total duration from spans
            try:
                starts = [datetime.fromisoformat(s["start_time"]) for s in self._spans if s.get("start_time")]
                if starts:
                    min_start = min(starts)
                    max_end = max(
                        datetime.fromisoformat(s["end_time"])
                        if s.get("end_time")
                        else datetime.fromisoformat(s["start_time"]) + timedelta(milliseconds=s.get("duration_ms", 0))
                        for s in self._spans
                        if s.get("start_time")
                    )
                    total_ms = round((max_end - min_start).total_seconds() * 1000, 2)
                    await TraceSummaryRepository.update_duration(self.trace_id, total_ms)
            except Exception:
                logger.debug("Could not compute total duration for trace %s", self.trace_id, exc_info=True)
        except Exception:
            logger.warning("Failed to flush %d trace spans", len(self._spans), exc_info=True)
        finally:
            self._spans.clear()


async def record_span(
    trace_id: str,
    span_name: str,
    start_time: str,
    duration_ms: float,
    agent_id: str | None = None,
    parent_span: str | None = None,
    status: str = "ok",
    metadata: dict | None = None,
    end_time: str | None = None,
) -> None:
    """Record a single span directly. Fire-and-forget."""
    try:
        await TraceSpanRepository.insert(
            trace_id=trace_id,
            span_name=span_name,
            start_time=start_time,
            duration_ms=duration_ms,
            agent_id=agent_id,
            parent_span=parent_span,
            status=status,
            metadata=sanitize_trace_metadata(metadata),
            end_time=end_time,
        )
    except Exception:
        logger.warning("Failed to record span %s", span_name, exc_info=True)


async def create_trace_summary(
    trace_id: str,
    conversation_id: str | None,
    user_input: str,
    final_response: str,
    routing_agent: str,
    routing_confidence: float | None,
    routing_duration_ms: float | None,
    condensed_task: str,
    agents: list[str],
    source: str,
    agent_instructions: dict[str, str] | None = None,
    conversation_turns: list[dict] | None = None,
    device_id: str | None = None,
    area_id: str | None = None,
    device_name: str | None = None,
    area_name: str | None = None,
    voice_followup: bool | None = None,
    verbatim_terms: list[str] | None = None,
) -> None:
    """Create a trace_summary record. Fire-and-forget.

    FLOW-CTX-1 (0.18.6): ``device_*``/``area_*`` identify which
    satellite originated the trace. They default to ``None`` so
    existing call sites (tests, unauthenticated REST) stay valid.
    """
    try:
        summary_agent_instructions = agent_instructions or {routing_agent: condensed_task}
        await TraceSummaryRepository.create(
            {
                "trace_id": trace_id,
                "conversation_id": conversation_id,
                "user_input": sanitize_trace_value(user_input, key="user_input"),
                "final_response": sanitize_trace_value(final_response, key="final_response"),
                "agents": agents,
                "total_duration_ms": None,
                "source": source,
                "routing_agent": routing_agent,
                "routing_confidence": routing_confidence,
                "routing_duration_ms": routing_duration_ms,
                "routing_reasoning": None,
                "agent_instructions": sanitize_trace_value(
                    summary_agent_instructions,
                    key="agent_instructions",
                ),
                "conversation_turns": sanitize_trace_value(
                    conversation_turns,
                    key="conversation_turns",
                ),
                "device_id": device_id,
                "area_id": area_id,
                "device_name": device_name,
                "area_name": area_name,
                "voice_followup": voice_followup,
                "verbatim_terms": sanitize_trace_value(
                    verbatim_terms,
                    key="verbatim_terms",
                ),
            }
        )
    except Exception:
        logger.warning("Failed to create trace summary for %s", trace_id, exc_info=True)


class _NoOpSpan:
    """No-op span for when span_collector is None.

    Accepts writes so callers that mutate a span do not crash.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def __setitem__(self, key, value):
        pass

    def __getitem__(self, key):
        return self._data.get(key, {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data

    def pop(self, key, default=None):
        return self._data.pop(key, default)


@contextlib.asynccontextmanager
async def _optional_span(span_collector, name, **kwargs):
    if span_collector:
        async with span_collector.start_span(name, **kwargs) as span:
            yield span
    else:
        yield _NoOpSpan()
