"""AI-task request and response models (``POST /api/ai_task``).

The Home Assistant bridge forwards ``ai_task.generate_data`` calls here: a
named task with instructions, an optional JSON schema (``structure``) and
optional image attachments (base64).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Contract limits.
AI_TASK_MAX_ATTACHMENTS = 4
AI_TASK_MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Total request cap: 4 base64-encoded 8 MiB images are about 44.7 MB of JSON
# string data, so the body limit leaves room for them plus the other fields.
AI_TASK_MAX_BODY_BYTES = 48 * 1024 * 1024


class AiTaskAttachment(BaseModel):
    """One image attachment; ``data`` is the base64-encoded image."""

    mime_type: str = Field(..., min_length=1, max_length=100)
    data: str = Field(..., min_length=1, repr=False)


class AiTaskRequest(BaseModel):
    """Incoming AI task from the HA bridge."""

    model_config = ConfigDict(extra="ignore")

    task_name: str = Field(..., min_length=1, max_length=200)
    instructions: str = Field(..., min_length=1, max_length=4000)
    structure: dict[str, Any] | None = Field(
        None, description="JSON schema (type: object) the result data must validate against"
    )
    attachments: list[AiTaskAttachment] = Field(default_factory=list, max_length=AI_TASK_MAX_ATTACHMENTS)
    conversation_id: str | None = Field(None, max_length=64)
    language: str | None = Field(None, max_length=10)
    user_id: str | None = Field(None, max_length=64)
    device_id: str | None = Field(None, max_length=64)

    @field_validator("attachments", mode="before")
    @classmethod
    def _none_attachments_to_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class AiTaskResponse(BaseModel):
    """AI-task result returned to the HA bridge."""

    conversation_id: str
    data: Any = Field(
        ...,
        description="JSON object validated against ``structure`` when given, else the result text",
    )
    text: str = Field(..., description="Unmediated agent result text")
    routed_agent: str | None = None
    action_executed: bool = False
