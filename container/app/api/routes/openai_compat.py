"""OpenAI-compatible chat completions API (Open WebUI ingress).

Exposes ``GET /v1/models`` and ``POST /v1/chat/completions`` so OpenAI API
clients such as Open WebUI can chat with HA-AgentHub. Every real turn is
converted into the same A2A request the conversation routes build and
dispatched to the orchestrator; this module only adapts the wire format.

Open WebUI specifics:

- Background tasks (title, tags, follow-up and search-query generation)
  are answered with static stubs and never reach the orchestrator, so they
  can not execute Home Assistant actions. They are detected from the
  ``X-OpenWebUI-Task`` header or, as a fallback, from the ``### Task:``
  prompt prefix Open WebUI uses for its task prompts. This is client
  protocol detection, not intent routing.
- With ``ENABLE_FORWARD_USER_INFO_HEADERS`` the ``X-OpenWebUI-User-*``
  headers identify the Open WebUI user; the user is recorded in
  ``external_user_mappings`` and the mapped Home Assistant user id (if
  any) is passed as ``user_id``. ``X-OpenWebUI-Chat-Id`` keys the
  server-side conversation history.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.conversation import _apply_stream_chunk, _build_a2a_request
from app.api.routes.dashboard_api import resolve_chat_language
from app.db.repository import ExternalUserMappingRepository
from app.middleware.rate_limit import rate_limit_conversation
from app.models.conversation import ConversationRequest, StreamToken
from app.security.auth import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["openai"])

MODEL_ID = "ha-agenthub"
# ``source`` value in ``external_user_mappings`` for Open WebUI users.
OPENWEBUI_SOURCE = "openwebui"
_CONVERSATION_ID_PREFIX = "owui-"

_HEADER_TASK = "x-openwebui-task"
_HEADER_CHAT_ID = "x-openwebui-chat-id"
_HEADER_USER_ID = "x-openwebui-user-id"
_HEADER_USER_NAME = "x-openwebui-user-name"
_HEADER_USER_EMAIL = "x-openwebui-user-email"

# Lower-cased X-OpenWebUI-Task values that a client sends for a regular chat
# turn when its {{TASK}} template renders to nothing meaningful.
_NO_TASK_HEADER_VALUES = frozenset({"none", "null", "false", "undefined"})
_TASK_PROMPT_PREFIX = "### Task:"
# Task section of an Open WebUI task prompt: text after "### Task:" up to
# the next "###" heading. Inference only inspects this section so words in
# the embedded chat history can not change the detected task type.
_TASK_SECTION_RE = re.compile(r"###\s*Task:(.*?)(?:\n\s*###|\Z)", re.IGNORECASE | re.DOTALL)
# Ordered (keyword, task type) pairs for the prompt-prefix fallback.
_TASK_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("follow-up", "follow_up_generation"),
    ("search queries", "query_generation"),
    ("tags", "tags_generation"),
    ("title", "title_generation"),
)
_TASK_STUBS: dict[str, str] = {
    "tags_generation": json.dumps({"tags": []}),
    "follow_up_generation": json.dumps({"follow_ups": []}),
    "query_generation": json.dumps({"queries": []}),
}
_TITLE_FALLBACK = "Chat"
_TITLE_MAX_WORDS = 6
_CHAT_HISTORY_USER_RE = re.compile(r"^\s*USER:\s*(.+)$", re.MULTILINE)

_MODEL_CREATED = int(time.time())

# The dispatcher is set by bootstrap during startup.
_dispatcher = None


def set_dispatcher(dispatcher) -> None:
    """Called at startup to inject the A2A dispatcher."""
    global _dispatcher
    _dispatcher = dispatcher


def _field_max_length(field_name: str) -> int | None:
    """Return the ``max_length`` constraint of a ``ConversationRequest`` field."""
    for meta in ConversationRequest.model_fields[field_name].metadata:
        max_length = getattr(meta, "max_length", None)
        if max_length is not None:
            return int(max_length)
    return None


_TEXT_MAX_LENGTH = _field_max_length("text")
_CONVERSATION_ID_MAX_LENGTH = _field_max_length("conversation_id")
_USER_ID_MAX_LENGTH = _field_max_length("user_id")


# --- Request models (permissive: unknown OpenAI fields are ignored) ---


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str = ""
    content: str | list[Any] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False


# --- Helpers ---


def _openai_error(status_code: int, message: str, code: str, error_type: str = "invalid_request_error"):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
    )


def _message_text(message: ChatMessage) -> str:
    """Return the text of a message; list content concatenates its text parts."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part["text"]
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def _header(request: Request, name: str) -> str:
    """Return a stripped header value, decoding UTF-8 and percent-encoding.

    HTTP header values arrive as latin-1; Open WebUI may send UTF-8 user
    names raw or percent-encoded, so both are normalized here.
    """
    raw = request.headers.get(name)
    if not raw:
        return ""
    with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
        raw = raw.encode("latin-1").decode("utf-8")
    return unquote(raw).strip()


def _infer_task_type(prompt: str) -> str:
    match = _TASK_SECTION_RE.search(prompt)
    section = (match.group(1) if match else "").lower()
    for keyword, task_type in _TASK_KEYWORDS:
        if keyword in section:
            return task_type
    return ""


def _is_no_task_header(value: str) -> bool:
    """True for a lower-cased ``X-OpenWebUI-Task`` value that means "no task"."""
    return value in _NO_TASK_HEADER_VALUES or ("{{" in value and "}}" in value)


def _detect_task(request: Request, last_user_text: str) -> tuple[str, bool] | None:
    """Return ``(task_type, from_header)`` for an Open WebUI background task.

    ``None`` means a regular chat turn. ``task_type`` may be ``""`` when the
    task is unknown. Header values that mean "no task" (empty, an unrendered
    ``{{...}}`` placeholder, ``none``/``null``/``false``/``undefined``) are
    ignored.
    """
    header_task = _header(request, _HEADER_TASK).lower()
    if header_task and not _is_no_task_header(header_task):
        return header_task, True
    if last_user_text.lstrip().startswith(_TASK_PROMPT_PREFIX):
        return _infer_task_type(last_user_text), False
    return None


def _derive_title(prompt: str) -> str:
    """First words of the original chat request from a title-task prompt."""
    match = _CHAT_HISTORY_USER_RE.search(prompt)
    if not match:
        return _TITLE_FALLBACK
    words = match.group(1).split()[:_TITLE_MAX_WORDS]
    return " ".join(words) or _TITLE_FALLBACK


def _task_stub_content(task_type: str, prompt: str, from_header: bool) -> str:
    if task_type == "title_generation":
        title = _derive_title(prompt) if from_header else _TITLE_FALLBACK
        return json.dumps({"title": title})
    return _TASK_STUBS.get(task_type, "")


def _conversation_id(request: Request, messages: list[ChatMessage]) -> str:
    """Server-side conversation key for the Open WebUI chat.

    Prefers ``X-OpenWebUI-Chat-Id``; otherwise derives a stable id from the
    user and the first user message of the chat.
    """
    chat_id = _header(request, _HEADER_CHAT_ID)
    if chat_id:
        conversation_id = _CONVERSATION_ID_PREFIX + chat_id
        return conversation_id[:_CONVERSATION_ID_MAX_LENGTH] if _CONVERSATION_ID_MAX_LENGTH else conversation_id
    user_key = _header(request, _HEADER_USER_ID) or "anon"
    first_user_text = next((_message_text(m) for m in messages if m.role == "user"), "")
    digest = hashlib.sha256(f"{user_key}\n{first_user_text}".encode()).hexdigest()[:32]
    return _CONVERSATION_ID_PREFIX + digest


async def _resolve_user_id(request: Request) -> str | None:
    """Record the Open WebUI user as seen and return the mapped HA user id."""
    external_user_id = _header(request, _HEADER_USER_ID)
    if not external_user_id:
        return None
    try:
        ha_user_id = await ExternalUserMappingRepository.touch(
            OPENWEBUI_SOURCE,
            external_user_id,
            display_name=_header(request, _HEADER_USER_NAME) or None,
            email=_header(request, _HEADER_USER_EMAIL) or None,
        )
    except Exception:
        logger.warning("Failed to record Open WebUI user; continuing without user mapping", exc_info=True)
        return None
    if ha_user_id and _USER_ID_MAX_LENGTH and len(ha_user_id) > _USER_ID_MAX_LENGTH:
        logger.warning("Mapped HA user id exceeds %d characters; ignoring mapping", _USER_ID_MAX_LENGTH)
        return None
    return ha_user_id


def _canned_error(error: Any) -> str:
    """User-facing error text; same wording as the HA bridge."""
    if isinstance(error, dict):
        error = error.get("message") or error.get("code") or "unknown error"
    return f"The assistant could not complete that request. ({error})"


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _completion_body(content: str) -> dict[str, Any]:
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class _ChunkWriter:
    """Formats ``chat.completion.chunk`` SSE events for one completion."""

    def __init__(self) -> None:
        self.id = _completion_id()
        self.created = int(time.time())
        self.role_sent = False

    def _event(self, delta: dict[str, Any], finish_reason: str | None) -> str:
        payload = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    def content(self, text: str) -> str:
        delta: dict[str, Any] = {"content": text}
        if not self.role_sent:
            delta = {"role": "assistant", "content": text}
            self.role_sent = True
        return self._event(delta, None)

    def finish(self) -> list[str]:
        events = [] if self.role_sent else [self.content("")]
        events.append(self._event({}, "stop"))
        events.append("data: [DONE]\n\n")
        return events


async def _static_stream(content: str):
    writer = _ChunkWriter()
    if content:
        yield writer.content(content)
    for event in writer.finish():
        yield event


async def _stream_turn(request: Request, a2a_request, span_collector):
    """Map orchestrator stream frames to OpenAI chunks.

    Mirrors the HA bridge (``custom_components/ha_agenthub/conversation.py``):

    - ``filler_push`` frames are skipped (no spoken preamble in a text chat)
      and status-only frames carry no token, so they emit nothing.
    - The terminal frame carries ``mediated_speech`` only when no tokens were
      streamed; it is emitted only in that case. If tokens were already
      streamed they are the answer, so a differing ``mediated_speech`` is
      not appended (that would duplicate or contradict visible text).
    - A terminal ``error`` is shown only when nothing else was emitted.
    - Leading whitespace is dropped: content is emitted only from the first
      non-whitespace character on (agents may open with blank lines).
    """
    root_span_id = getattr(request.state, "root_span_id", None)
    parent_token = None
    if span_collector and root_span_id:
        parent_token = span_collector.push_parent(root_span_id)
    writer = _ChunkWriter()
    t0 = time.perf_counter()
    first_frame_ms: float | None = None
    frame = StreamToken(token="")  # nosec B106
    streamed = ""
    mediated = ""
    finished = False
    content_started = False
    try:
        try:
            async for chunk in _dispatcher.dispatch_stream(a2a_request):
                now_ms = (time.perf_counter() - t0) * 1000
                if first_frame_ms is None:
                    first_frame_ms = now_ms
                    request.state.first_frame_ms = first_frame_ms
                if finished:
                    continue
                frame = _apply_stream_chunk(
                    frame,
                    chunk,
                    first_frame_ms=first_frame_ms,
                    now_ms=now_ms,
                    trace_id=getattr(request.state, "trace_id", None),
                )
                if frame.filler_push is not None:
                    continue
                text = frame.token
                streamed += text
                if frame.done:
                    finished = True
                    if frame.mediated_speech and not streamed.strip():
                        mediated = frame.mediated_speech
                        text += mediated
                    if frame.error and not (mediated or streamed).strip():
                        logger.warning("Container reported error in OpenAI stream done chunk: %s", frame.error)
                        text += _canned_error(frame.error)
                if not content_started:
                    text = text.lstrip()
                if text:
                    content_started = True
                    yield writer.content(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("OpenAI-compatible stream failed", exc_info=True)
            if not writer.role_sent:
                yield writer.content(_canned_error("internal error"))
        for event in writer.finish():
            yield event
    finally:
        if span_collector and parent_token is not None:
            span_collector.pop_parent(parent_token)
        if span_collector:
            await span_collector.flush()


# --- Endpoints ---


@router.get("/v1/models")
async def list_models(_: str = Depends(require_api_key)) -> dict[str, Any]:
    """List the single model id clients select to talk to HA-AgentHub."""
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": _MODEL_CREATED, "owned_by": MODEL_ID}],
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    _: str = Depends(require_api_key),
):
    """OpenAI chat completion: the last user message becomes one orchestrator turn.

    System messages and prior history in ``messages`` are ignored; the
    server-side history keyed by the conversation id is authoritative.
    Background-task stubs are answered before the conversation rate limit is
    applied, so only real chat turns consume it.
    """
    user_messages = [m for m in body.messages if m.role == "user"]
    last_user_text = _message_text(user_messages[-1]) if user_messages else ""

    task = _detect_task(request, last_user_text)
    if task is not None:
        task_type, from_header = task
        content = _task_stub_content(task_type, last_user_text, from_header)
        if body.stream:
            return StreamingResponse(_static_stream(content), media_type="text/event-stream")
        return _completion_body(content)

    # Raises HTTPException(429) when the per-IP conversation limit is exceeded.
    await rate_limit_conversation(request)

    text = last_user_text.strip()
    if not text:
        return _openai_error(400, "No user message text provided", "empty_message")
    if _TEXT_MAX_LENGTH is not None and len(text) > _TEXT_MAX_LENGTH:
        return _openai_error(
            400,
            f"Message exceeds the maximum length of {_TEXT_MAX_LENGTH} characters",
            "text_too_long",
        )
    if _dispatcher is None:
        return _openai_error(503, "Service not ready", "service_unavailable", error_type="server_error")

    conv_request = ConversationRequest(
        text=text,
        conversation_id=_conversation_id(request, body.messages),
        # Same language resolution as the dashboard chat: the ``language``
        # setting (``auto`` = detect from the user input).
        language=await resolve_chat_language(),
        user_id=await _resolve_user_id(request),
    )
    span_collector = getattr(request.state, "span_collector", None)

    if body.stream:
        a2a_request, _task = _build_a2a_request(conv_request, "message/stream", span_collector, request)
        return StreamingResponse(_stream_turn(request, a2a_request, span_collector), media_type="text/event-stream")

    a2a_request, _task = _build_a2a_request(conv_request, "message/send", span_collector, request)
    try:
        response = await _dispatcher.dispatch(a2a_request)
    except RuntimeError as exc:
        return _completion_body(f"Error: {exc}")
    result = response or {}
    content = (result.get("speech") or "").strip()
    if not content and result.get("error"):
        content = _canned_error(result["error"])
    return _completion_body(content)
