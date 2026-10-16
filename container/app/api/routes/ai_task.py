"""AI-task endpoint (``POST /api/ai_task``) for Home Assistant ``ai_task.generate_data``.

Flow: validate the request (schema, attachments) off the event loop ->
optional vision pre-step (image attachments -> text description) -> one
non-streaming orchestrator turn with source ``ai_task`` (normal
classification, routing and device actions; no cache, mediation, filler,
follow-up or history) -> optional structured formatter (JSON schema).

Image bytes stay in the vision pre-step: they are never logged, traced,
stored, or placed into A2A envelopes; only the text description travels on.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from app.a2a._request import build_send_request
from app.agents.task_formatter import TaskFormatError, check_task_schema, format_structured
from app.agents.task_vision import (
    MAX_DESCRIPTION_CHARS,
    TaskImage,
    TaskVisionError,
    build_task_text,
    describe_images,
)
from app.middleware.rate_limit import rate_limit_conversation
from app.models.agent import IngressTask, TaskContext
from app.models.ai_task import (
    AI_TASK_MAX_BODY_BYTES,
    AI_TASK_MAX_IMAGE_BYTES,
    AiTaskRequest,
    AiTaskResponse,
)
from app.security.auth import body_size_limit, require_api_key
from app.security.user_input import prepare_user_text

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai_task"])

# The dispatcher is set by the startup bootstrap.
_dispatcher = None

# Orchestrator input bound for a task: instructions (max 4000) plus the
# delimited image description (max MAX_DESCRIPTION_CHARS) plus its label.
AI_TASK_MAX_TEXT_CHARS = 4000 + MAX_DESCRIPTION_CHARS + 500

# Timer directives delegate to HA's built-in conversation agent, which an AI
# task cannot reach: the task returns this text instead (HTTP 200).
DIRECTIVE_NOT_SUPPORTED_TEXT = (
    "This task needs the Home Assistant built-in assistant (for example for a timer), "
    "which is not supported in AI tasks."
)
_DISPATCH_FAILED_DETAIL = "The task could not be processed by the orchestrator."
_TASK_FAILED_DETAIL = "The task failed."
AI_TASK_MAX_TASK_NAME_CHARS = 200

_IMAGE_MIME_RE = re.compile(r"^image/[a-z0-9][a-z0-9.+-]*$")
_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
# Largest base64 text that can decode to at most AI_TASK_MAX_IMAGE_BYTES.
_MAX_BASE64_CHARS = 4 * ((AI_TASK_MAX_IMAGE_BYTES + 2) // 3)


def set_dispatcher(dispatcher) -> None:
    """Called by the startup bootstrap to inject the A2A dispatcher."""
    global _dispatcher
    _dispatcher = dispatcher


class AiTaskInputError(ValueError):
    """Invalid AI-task input; the message is safe to return as a 400 detail."""


def _describe_validation_error(exc: ValidationError) -> str:
    """First validation error as ``field: message`` (never echoes the input value)."""
    errors = exc.errors(include_input=False, include_url=False)
    if not errors:
        return "Invalid request"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body") or "body"
    return f"{location}: {first.get('msg', 'invalid value')}"


def _decode_attachment(index: int, mime_type: str, data: str) -> TaskImage:
    mime = mime_type.strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    if not _IMAGE_MIME_RE.match(mime):
        raise AiTaskInputError(f"attachments[{index}]: only image/* mime types are supported")
    compact = "".join(data.split())
    if len(compact) > _MAX_BASE64_CHARS:
        raise AiTaskInputError(f"attachments[{index}]: image exceeds the 8 MB limit")
    try:
        decoded_size = len(base64.b64decode(compact, validate=True))
    except (binascii.Error, ValueError):
        raise AiTaskInputError(f"attachments[{index}]: data is not valid base64") from None
    if decoded_size == 0:
        raise AiTaskInputError(f"attachments[{index}]: data is empty")
    if decoded_size > AI_TASK_MAX_IMAGE_BYTES:
        raise AiTaskInputError(f"attachments[{index}]: image exceeds the 8 MB limit")
    return TaskImage(mime_type=mime, data=compact, size_bytes=decoded_size)


def parse_ai_task_request(raw: bytes | bytearray) -> tuple[AiTaskRequest, list[TaskImage]]:
    """Parse and validate an AI-task body (CPU-bound: run off the event loop)."""
    try:
        payload = json.loads(raw)
    except ValueError:
        raise AiTaskInputError("Request body must be valid JSON") from None
    if not isinstance(payload, dict):
        raise AiTaskInputError("Request body must be a JSON object")
    try:
        task_request = AiTaskRequest.model_validate(payload)
    except ValidationError as exc:
        raise AiTaskInputError(_describe_validation_error(exc)) from None
    if not task_request.task_name.strip():
        raise AiTaskInputError("task_name: must not be blank")
    if not task_request.instructions.strip():
        raise AiTaskInputError("instructions: must not be blank")
    if task_request.structure is not None:
        try:
            check_task_schema(task_request.structure)
        except ValueError as exc:
            raise AiTaskInputError(str(exc)) from None
    images = [
        _decode_attachment(index, attachment.mime_type, attachment.data)
        for index, attachment in enumerate(task_request.attachments)
    ]
    # The validated payloads now live only in ``images``; drop the request
    # model's base64 copies so they are not kept alive through the turn.
    task_request.attachments = []
    return task_request, images


async def _read_body_limited(request: Request, limit: int) -> bytearray:
    """Read the request body, failing with 413 as soon as it exceeds ``limit`` bytes.

    Streams the body so a chunked request without ``Content-Length`` is never
    buffered beyond the limit.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
    return body


def _action_was_executed(raw: Any) -> bool:
    """True when the turn reports a successful Home Assistant action."""
    if raw is None:
        return False
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if isinstance(raw, dict):
        return bool(raw.get("success", bool(raw.get("service") or raw.get("action"))))
    return bool(raw)


def _build_task_request(
    task_request: AiTaskRequest,
    text: str,
    description: str | None,
    span_collector: Any,
):
    prepared = prepare_user_text(text, max_length=AI_TASK_MAX_TEXT_CHARS)
    context = TaskContext(
        device_id=task_request.device_id,
        user_id=task_request.user_id,
        language=task_request.language or "en",
        # Always ``ai_task``: the stateless-task exemptions must never depend
        # on the trace source the middleware derived from the path.
        source="ai_task",
        injection_detected=prepared.injection_detected,
        attachment_description=description,
    )
    task = IngressTask(
        description=prepared.text,
        conversation_id=task_request.conversation_id,
        context=context,
    )
    return build_send_request(
        "orchestrator",
        task,
        request_id=str(uuid.uuid4()),
        span_collector=span_collector,
    )


@router.post(
    "/api/ai_task",
    response_model=AiTaskResponse,
    dependencies=[Depends(rate_limit_conversation), Depends(body_size_limit(AI_TASK_MAX_BODY_BYTES))],
)
async def ai_task(request: Request, _: str = Depends(require_api_key)) -> AiTaskResponse:
    """Run one Home Assistant AI task through the orchestrator."""
    raw = await _read_body_limited(request, AI_TASK_MAX_BODY_BYTES)
    try:
        task_request, images = await asyncio.to_thread(parse_ai_task_request, raw)
    except AiTaskInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        del raw
    if _dispatcher is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    span_collector = getattr(request.state, "span_collector", None)
    trace_id = getattr(request.state, "trace_id", None)
    # Sanitize once with the orchestrator-path sanitizer; every step (vision,
    # orchestrator, formatter) receives the same sanitized text.
    task_name = prepare_user_text(task_request.task_name, max_length=AI_TASK_MAX_TASK_NAME_CHARS).text
    instructions = prepare_user_text(task_request.instructions, max_length=AI_TASK_MAX_TEXT_CHARS).text
    if not task_name or not instructions:
        images.clear()
        raise HTTPException(status_code=400, detail="task_name and instructions must contain text")

    description: str | None = None
    if images:
        try:
            description = await describe_images(
                task_name,
                instructions,
                images,
                span_collector=span_collector,
            )
        except TaskVisionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        finally:
            # Drop the payload references as soon as the vision step is done.
            images.clear()

    a2a_request = _build_task_request(
        task_request,
        build_task_text(instructions, description),
        description,
        span_collector,
    )
    try:
        result = await _dispatcher.dispatch(a2a_request)
    except (RuntimeError, TimeoutError):
        logger.warning("AI task dispatch failed (trace_id=%s)", trace_id, exc_info=True)
        raise HTTPException(status_code=502, detail=_DISPATCH_FAILED_DETAIL) from None
    result = result if isinstance(result, dict) else {}

    if result.get("directive"):
        text = DIRECTIVE_NOT_SUPPORTED_TEXT
        action_executed = False
    else:
        text = str(result.get("task_text") or result.get("speech") or "")
        action_executed = _action_was_executed(result.get("action_executed"))
        if result.get("error"):
            # Canned failure / all agents failed: never hand the failure line
            # to the formatter or return it as successful task data.
            logger.info("AI task turn ended with error %r (trace_id=%s)", result.get("error"), trace_id)
            raise HTTPException(status_code=502, detail=text or _TASK_FAILED_DETAIL)

    data: Any = text
    if task_request.structure is not None:
        try:
            data = await format_structured(
                task_name=task_name,
                instructions=instructions,
                result_text=text,
                action_executed=action_executed,
                schema=task_request.structure,
                span_collector=span_collector,
            )
        except TaskFormatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    return AiTaskResponse(
        conversation_id=str(result.get("conversation_id") or task_request.conversation_id or uuid.uuid4().hex),
        data=data,
        text=text,
        routed_agent=result.get("routed_to"),
        action_executed=action_executed,
    )
