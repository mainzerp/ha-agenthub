"""Log ingest API: accepts batched log records shipped by the HA integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.middleware.rate_limit import rate_limit_log_ingest
from app.security.auth import body_size_limit, require_api_key
from app.util.log_buffer import get_log_buffer

router = APIRouter(
    prefix="/api/logs",
    tags=["logs-ingest"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LogIngestRecord(BaseModel):
    """One shipped log record (wire schema of the HA log shipper)."""

    timestamp: datetime
    level: str = Field(..., pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    name: str = Field(..., max_length=128)
    message: str = Field(..., max_length=2000)
    lineno: int = Field(0, ge=0)
    module: str = ""
    funcName: str = ""  # noqa: N815
    trace_id: str | None = Field(None, max_length=64)
    conversation_id: str | None = Field(None, max_length=64)

    @field_validator("timestamp")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps: LogBuffer.get_entries compares them against
        tz-aware datetimes, so a naive value would 500 the admin logs view."""
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/ingest")
async def ingest_logs(
    records: list[LogIngestRecord] = Body(..., max_length=100),
    _: None = Depends(body_size_limit(256_000)),
    __: None = Depends(rate_limit_log_ingest),
) -> dict[str, int]:
    """Append a batch of shipped log records to the in-memory log buffer."""
    buf = get_log_buffer()
    if buf is None:
        raise HTTPException(status_code=503, detail="log buffer unavailable")

    for r in records:
        entry: dict[str, Any] = {
            "timestamp": r.timestamp.isoformat(),
            "level": r.level,
            "name": r.name,
            "message": r.message,
            "lineno": r.lineno,
            "module": r.module,
            "funcName": r.funcName,
            "source": "ha",
        }
        # Optional keys are only included when set, keeping the key set of
        # ingested entries aligned with locally captured ones.
        if r.trace_id is not None:
            entry["trace_id"] = r.trace_id
        if r.conversation_id is not None:
            entry["conversation_id"] = r.conversation_id
        buf.add_entry(entry)

    return {"accepted": len(records)}
