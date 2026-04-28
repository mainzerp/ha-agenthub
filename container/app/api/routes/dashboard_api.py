"""Admin dashboard API endpoints.

Provides data for the HTMX-powered dashboard pages: overview metrics,
agent CRUD, prompt editing, extended health, and rewrite configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from datetime import UTC
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.a2a.protocol import JsonRpcRequest
from app.db.repository import (
    AgentConfigRepository,
    AnalyticsRepository,
    ConversationRepository,
    SendDeviceMappingRepository,
    SettingsRepository,
    TraceSummaryRepository,
)
from app.models.agent import AgentTask, TaskContext
from app.models.conversation import StreamToken
from app.runtime_setup import ensure_setup_runtime_initialized
from app.security.auth import require_admin_session
from app.security.user_input import prepare_user_text

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-dashboard"],
    dependencies=[Depends(require_admin_session)],
)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

_SAFE_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# --- Chat dispatcher (injected by main.py) ---

_dispatcher = None


def set_chat_dispatcher(dispatcher) -> None:
    """Called by main.py to inject the A2A dispatcher for chat bridge."""
    global _dispatcher
    _dispatcher = dispatcher


def _create_phase2_agent(agent_id: str, app):
    """Instantiate a Phase 2 agent by ID for hot-registration."""
    from app.agents.automation import AutomationAgent
    from app.agents.climate import ClimateAgent
    from app.agents.media import MediaAgent
    from app.agents.scene import SceneAgent
    from app.agents.security import SecurityAgent
    from app.agents.send import SendAgent
    from app.agents.timer import TimerAgent

    agent_map = {
        "timer-agent": TimerAgent,
        "climate-agent": ClimateAgent,
        "media-agent": MediaAgent,
        "scene-agent": SceneAgent,
        "automation-agent": AutomationAgent,
        "security-agent": SecurityAgent,
        "send-agent": SendAgent,
    }
    with_matcher = {"climate-agent", "security-agent", "timer-agent", "scene-agent", "automation-agent", "media-agent"}

    cls = agent_map.get(agent_id)
    if cls is None:
        return None

    ha_client = getattr(app.state, "ha_client", None)
    entity_index = getattr(app.state, "entity_index", None)
    entity_matcher = getattr(app.state, "entity_matcher", None)

    if agent_id in with_matcher:
        return cls(ha_client=ha_client, entity_index=entity_index, entity_matcher=entity_matcher)
    return cls(ha_client=ha_client, entity_index=entity_index)


def _validate_agent_path(agent_id: str) -> Path:
    """Validate agent_id and return a safe prompt path within PROMPTS_DIR."""
    if not _SAFE_AGENT_ID_RE.match(agent_id):
        raise ValueError("Invalid agent ID")
    filename = agent_id.replace("-agent", "") + ".txt"
    prompt_path = (PROMPTS_DIR / filename).resolve()
    if not str(prompt_path).startswith(str(PROMPTS_DIR.resolve())):
        raise ValueError("Invalid agent ID")
    return prompt_path


# --- Request models ---


class AgentConfigUpdate(BaseModel):
    enabled: bool | None = None
    model: str | None = None
    timeout: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_iterations: int | None = None
    description: str | None = None
    reasoning_effort: str | None = None


class PromptUpdate(BaseModel):
    content: str


class RewriteConfigUpdate(BaseModel):
    model: str | None = None
    temperature: float | None = None


class PersonalityConfigUpdate(BaseModel):
    prompt: str | None = None
    mediation_temperature: float | None = None
    filler_enabled: bool | None = None
    filler_threshold_ms: int | None = None


# --- Overview ---


@router.get("/overview")
async def get_overview(request: Request):
    """Aggregated overview metrics for the dashboard home page."""
    await ensure_setup_runtime_initialized(request.app)
    registry = request.app.state.registry
    entity_index = request.app.state.entity_index
    cache_manager = request.app.state.cache_manager
    mcp_registry = request.app.state.mcp_registry

    agents = await registry.list_agents() if registry else []

    if cache_manager:
        with contextlib.suppress(Exception):
            cache_manager.get_stats()

    entity_count = 0
    if entity_index:
        try:
            stats = entity_index.get_stats()
            entity_count = stats.get("count", 0)
        except Exception:
            pass

    mcp_count = 0
    if mcp_registry:
        try:
            servers = mcp_registry.list_servers()
            mcp_count = len(servers)
        except Exception:
            pass

    # Count recent requests from analytics (last 24h)
    recent_requests = 0
    try:
        from datetime import datetime, timedelta

        start = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        events = await AnalyticsRepository.query_by_range(
            event_type="request",
            start=start,
            limit=100000,
        )
        recent_requests = len(events)
    except Exception:
        pass

    # Compute cache hit rate from analytics DB (cache tier stats don't track hits/queries)
    cache_hit_rate = 0
    try:
        from datetime import datetime, timedelta

        start_cache = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        cache_events = await AnalyticsRepository.query_by_range(
            event_type="cache",
            start=start_cache,
            limit=100000,
        )
        if cache_events:
            total_lookups = len(cache_events)
            hits = sum(1 for e in cache_events if e.get("hit_type", "") in ("routing_hit", "action_hit"))
            cache_hit_rate = round(hits / total_lookups * 100, 1)
    except Exception:
        pass
    hit_rate = cache_hit_rate

    return {
        "recent_requests": recent_requests,
        "cache_hit_rate": hit_rate,
        "agent_count": len(agents),
        "entity_count": entity_count,
        "mcp_server_count": mcp_count,
    }


@router.get("/overview/extended")
async def get_overview_extended(request: Request):
    """Aggregated overview data for the redesigned dashboard home page.

    Returns all data the overview needs in a single call: metrics, agent
    distribution, cache tier stats, recent traces, and error/warning info.
    """
    await ensure_setup_runtime_initialized(request.app)
    registry = request.app.state.registry
    entity_index = request.app.state.entity_index
    mcp_registry = request.app.state.mcp_registry

    from collections import defaultdict
    from datetime import datetime, timedelta

    start_24h = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

    # --- Basic counts (reuse existing overview logic) ---
    agents = await registry.list_agents() if registry else []

    entity_count = 0
    if entity_index:
        try:
            stats = entity_index.get_stats()
            entity_count = stats.get("count", 0)
        except Exception:
            pass

    mcp_count = 0
    if mcp_registry:
        try:
            servers = mcp_registry.list_servers()
            mcp_count = len(servers)
        except Exception:
            pass

    # --- Analytics: requests, latency ---
    requests = []
    with contextlib.suppress(Exception):
        requests = await AnalyticsRepository.query_by_range(
            event_type="request",
            start=start_24h,
            limit=100000,
        )

    recent_requests = len(requests)
    latencies = [
        r["data"]["latency_ms"]
        for r in requests
        if r.get("data") and isinstance(r["data"], dict) and "latency_ms" in r["data"]
    ]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0

    # --- Cache events (all non-request events for cache analysis) ---
    all_events = []
    with contextlib.suppress(Exception):
        all_events = await AnalyticsRepository.query_by_range(
            start=start_24h,
            limit=100000,
        )

    hit_types = {"routing_hit", "action_hit"}
    miss_types = {"miss"}
    hits = sum(1 for e in all_events if e.get("event_type") in hit_types)
    misses = sum(1 for e in all_events if e.get("event_type") in miss_types)
    total_cache = hits + misses
    cache_hit_rate = round(hits / total_cache * 100, 1) if total_cache > 0 else 0

    # Cache tier breakdown counts
    routing_hits = sum(1 for e in all_events if e.get("event_type") == "routing_hit")
    action_hits = sum(1 for e in all_events if e.get("event_type") == "action_hit")
    cache_misses = misses

    # Conversations count
    total_conversations = 0
    with contextlib.suppress(Exception):
        total_conversations = await ConversationRepository.count()

    # --- Agent distribution ---
    agent_counts: dict[str, int] = defaultdict(int)
    agent_latencies_map: dict[str, list] = defaultdict(list)
    for e in requests:
        agent = e.get("agent_id") or "unknown"
        agent_counts[agent] += 1
        data = e.get("data")
        if isinstance(data, dict) and "latency_ms" in data:
            agent_latencies_map[agent].append(data["latency_ms"])

    agent_distribution = []
    for agent_id in sorted(agent_counts.keys()):
        lats = agent_latencies_map.get(agent_id, [])
        agent_distribution.append(
            {
                "agent_id": agent_id,
                "request_count": agent_counts[agent_id],
                "avg_latency_ms": round(sum(lats) / len(lats), 1) if lats else 0,
            }
        )

    # --- Request time-series (hourly buckets, last 24h) ---
    request_buckets: dict[str, int] = defaultdict(int)
    bucket_minutes = 60
    for e in requests:
        ts = e.get("created_at", "")
        try:
            dt = datetime.fromisoformat(ts)
            bucket_secs = bucket_minutes * 60
            ts_epoch = int(dt.timestamp())
            bucket_start = ts_epoch - (ts_epoch % bucket_secs)
            bucket_label = datetime.fromtimestamp(bucket_start, tz=UTC).strftime("%H:%M")
            request_buckets[bucket_label] += 1
        except (ValueError, TypeError):
            pass

    request_labels = sorted(request_buckets.keys())
    request_data = [request_buckets[lb] for lb in request_labels]
    request_trend = {"labels": request_labels, "data": request_data}

    # --- Recent traces (last 8) ---
    recent_traces = []
    try:
        result = await TraceSummaryRepository.list_filtered(
            page=1,
            per_page=8,
        )
        for t in result:
            recent_traces.append(
                {
                    "trace_id": t.get("trace_id", ""),
                    "created_at": t.get("created_at", ""),
                    "user_input": (t.get("user_input") or "")[:120],
                    "routing_agent": t.get("routing_agent", ""),
                    "total_duration_ms": t.get("total_duration_ms", 0),
                    "label": t.get("label", ""),
                }
            )
    except Exception:
        pass

    # --- Errors/warnings ---
    agent_timeouts = sum(1 for e in all_events if e.get("event_type") == "agent_timeout")
    rewrite_events = [e for e in all_events if e.get("event_type") == "rewrite_invocation"]
    rewrite_failures = sum(
        1
        for e in rewrite_events
        if e.get("data") and isinstance(e["data"], dict) and not e["data"].get("success", True)
    )

    return {
        "recent_requests": recent_requests,
        "cache_hit_rate": cache_hit_rate,
        "agent_count": len(agents),
        "entity_count": entity_count,
        "mcp_server_count": mcp_count,
        "avg_latency_ms": avg_latency,
        "total_conversations": total_conversations,
        "agent_distribution": agent_distribution,
        "cache_tier": {
            "routing_hits": routing_hits,
            "action_hits": action_hits,
            "misses": cache_misses,
        },
        "request_trend": request_trend,
        "recent_traces": recent_traces,
        "warnings": {
            "agent_timeouts": agent_timeouts,
            "rewrite_failures": rewrite_failures,
        },
    }


# --- Agent CRUD ---


@router.get("/agents/{agent_id}")
async def get_agent_config(agent_id: str):
    """Get a single agent configuration."""
    config = await AgentConfigRepository.get(agent_id)
    if config is None:
        return JSONResponse(status_code=404, content={"detail": "Agent not found"})
    return config


@router.put("/agents/{agent_id}")
async def update_agent_config(agent_id: str, payload: AgentConfigUpdate, request: Request):
    """Update agent configuration fields."""
    updates = payload.model_dump(exclude_none=True)
    # Allow clearing reasoning_effort by sending empty string -> store as None
    if payload.reasoning_effort is not None:
        updates["reasoning_effort"] = payload.reasoning_effort or None
    if not updates:
        return {"status": "no changes"}
    try:
        await AgentConfigRepository.upsert(agent_id, **updates)

        # Hot-register/unregister agent in live registry
        if payload.enabled is not None:
            _registry = request.app.state.registry
            if payload.enabled:
                existing = await _registry.discover(agent_id)
                if existing is None:
                    agent_instance = _create_phase2_agent(agent_id, request.app)
                    if agent_instance:
                        await _registry.register(agent_instance)
                        logger.info("Hot-registered agent: %s", agent_id)
            else:
                core_agents = {"orchestrator", "general-agent", "light-agent", "music-agent", "rewrite-agent"}
                if agent_id not in core_agents:
                    await _registry.unregister(agent_id)
                    logger.info("Hot-unregistered agent: %s", agent_id)
    except Exception as exc:
        logger.exception("Failed to update agent config for %s", agent_id)
        return JSONResponse(status_code=500, content={"detail": str(exc) or "Failed to update agent"})

    return {"status": "ok", "agent_id": agent_id}


# --- Prompt read/write ---


@router.get("/agents/{agent_id}/prompt")
async def get_agent_prompt(agent_id: str):
    """Read the prompt file for an agent."""
    try:
        prompt_path = _validate_agent_path(agent_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid agent ID"})
    if not prompt_path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Prompt file not found"})
    content = await asyncio.to_thread(prompt_path.read_text, encoding="utf-8")
    filename = prompt_path.name
    return {"agent_id": agent_id, "filename": filename, "content": content}


@router.put("/agents/{agent_id}/prompt")
async def update_agent_prompt(agent_id: str, payload: PromptUpdate):
    """Write prompt file content for an agent."""
    try:
        prompt_path = _validate_agent_path(agent_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid agent ID"})
    try:
        await asyncio.to_thread(prompt_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(prompt_path.write_text, payload.content, encoding="utf-8")
    except Exception as exc:
        logger.exception("Failed to update prompt for %s", agent_id)
        return JSONResponse(status_code=500, content={"detail": str(exc) or "Failed to update prompt"})
    filename = prompt_path.name
    return {"status": "ok", "agent_id": agent_id, "filename": filename}


# --- Extended health ---


@router.get("/health/extended")
async def get_extended_health(request: Request):
    """Extended health check for all subsystems."""
    await ensure_setup_runtime_initialized(request.app)
    ha_client = request.app.state.ha_client
    entity_index = request.app.state.entity_index
    cache_manager = request.app.state.cache_manager
    mcp_registry = request.app.state.mcp_registry
    startup_time = getattr(request.app.state, "startup_time", None)

    components = {}

    # HA connection
    try:
        if ha_client:
            await ha_client.get_states()
            components["ha_connection"] = {"status": "healthy"}
        else:
            components["ha_connection"] = {"status": "error", "detail": "Not initialized"}
    except Exception as exc:
        components["ha_connection"] = {"status": "error", "detail": str(exc)}

    # Entity index
    try:
        if entity_index:
            stats = entity_index.get_stats()
            embedding_status = stats.get("embedding_status", {})
            state = embedding_status.get("state", "ready")
            if state in {"building", "syncing"}:
                components["entity_index"] = {
                    "status": "warning",
                    "detail": f"{state} ({embedding_status.get('processed', 0)}/{embedding_status.get('total', 0)})",
                    "progress": embedding_status.get("progress", 0),
                }
            elif state == "error":
                components["entity_index"] = {
                    "status": "error",
                    "detail": embedding_status.get("error") or "Index build failed",
                }
            else:
                components["entity_index"] = {"status": "healthy", "count": stats.get("count", 0)}
        else:
            components["entity_index"] = {"status": "error", "detail": "Not initialized"}
    except Exception as exc:
        components["entity_index"] = {"status": "error", "detail": str(exc)}

    # Cache
    try:
        if cache_manager:
            stats = cache_manager.get_stats()
            components["cache"] = {"status": "healthy", "stats": stats}
        else:
            components["cache"] = {"status": "error", "detail": "Not initialized"}
    except Exception as exc:
        components["cache"] = {"status": "error", "detail": str(exc)}

    # MCP servers
    try:
        if mcp_registry:
            servers = mcp_registry.list_servers()
            components["mcp_servers"] = {"status": "healthy", "count": len(servers)}
        else:
            components["mcp_servers"] = {"status": "error", "detail": "Not initialized"}
    except Exception as exc:
        components["mcp_servers"] = {"status": "error", "detail": str(exc)}

    # Uptime
    if startup_time:
        uptime_s = int(time.time() - startup_time)
        hours, remainder = divmod(uptime_s, 3600)
        minutes, seconds = divmod(remainder, 60)
        components["uptime"] = {"status": "healthy", "seconds": uptime_s, "display": f"{hours}h {minutes}m {seconds}s"}
    else:
        components["uptime"] = {"status": "unknown"}

    return components


# --- Rewrite config ---


@router.get("/rewrite/config")
async def get_rewrite_config():
    """Get current rewrite agent settings."""
    model = await SettingsRepository.get_value("rewrite.model", "")
    temperature = await SettingsRepository.get_value("rewrite.temperature", "0.7")
    return {
        "model": model or "",
        "temperature": float(temperature),
    }


@router.put("/rewrite/config")
async def update_rewrite_config(payload: RewriteConfigUpdate):
    """Update rewrite agent settings."""
    if payload.model is not None:
        await SettingsRepository.set(
            "rewrite.model",
            payload.model,
            "string",
            "rewrite",
            "Rewrite LLM model",
        )
    if payload.temperature is not None:
        await SettingsRepository.set(
            "rewrite.temperature",
            str(payload.temperature),
            "float",
            "rewrite",
            "Rewrite temperature",
        )
    return {"status": "ok"}


# --- Personality config ---


@router.get("/personality/config")
async def get_personality_config():
    """Get current personality prompt, mediation temperature, and filler settings."""
    prompt = await SettingsRepository.get_value("personality.prompt", "")
    temperature = await SettingsRepository.get_value("mediation.temperature", "0.3")
    filler_enabled = await SettingsRepository.get_value("filler.enabled", "false")
    filler_threshold_ms = await SettingsRepository.get_value("filler.threshold_ms", "1000")
    return {
        "prompt": prompt,
        "mediation_temperature": float(temperature),
        "filler_enabled": filler_enabled == "true",
        "filler_threshold_ms": int(filler_threshold_ms),
    }


@router.put("/personality/config")
async def update_personality_config(payload: PersonalityConfigUpdate):
    """Save personality prompt, mediation temperature, and filler settings."""
    if payload.prompt is not None:
        await SettingsRepository.set(
            "personality.prompt",
            payload.prompt,
            "string",
            "personality",
            "Personality system prompt for response mediation",
        )
    if payload.mediation_temperature is not None:
        await SettingsRepository.set(
            "mediation.temperature",
            str(payload.mediation_temperature),
            "float",
            "mediation",
            "Temperature for personality mediation LLM calls",
        )
    if payload.filler_enabled is not None:
        await SettingsRepository.set(
            "filler.enabled",
            str(payload.filler_enabled).lower(),
            "bool",
            "filler",
            "Enable interim filler responses for slow agents",
        )
    if payload.filler_threshold_ms is not None:
        await SettingsRepository.set(
            "filler.threshold_ms",
            str(payload.filler_threshold_ms),
            "int",
            "filler",
            "Milliseconds to wait before sending filler",
        )
    return {"status": "ok"}


# --- Chat bridge ---


class ChatRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    language: str | None = None


@router.post("/chat")
async def admin_chat(request: Request, payload: ChatRequest):
    """Bridge: session-auth chat -> internal conversation pipeline."""
    if _dispatcher is None:
        return JSONResponse(status_code=503, content={"detail": "Dispatcher not ready"})

    # FLOW-MED-9: source is now set by TracingMiddleware from the
    # route path (``/api/admin/chat`` -> ``"chat"``).
    span_collector = getattr(request.state, "span_collector", None)
    language = payload.language
    if not language:
        language = await SettingsRepository.get_value("language") or "en"
    prepared_text = prepare_user_text(payload.text)
    task = AgentTask(
        description=prepared_text.text,
        user_text=prepared_text.text,
        conversation_id=payload.conversation_id,
        # FLOW-CTX-1 (0.18.6): dashboard chat has no satellite and
        # no area. Mark ``source="chat"`` so agents can skip
        # area-based tie-breaking that would otherwise silently
        # pin to a previous request's area.
        context=TaskContext(language=language, source="chat", injection_detected=prepared_text.injection_detected),
    )
    a2a_request = JsonRpcRequest(
        method="message/send",
        params={
            "agent_id": "orchestrator",
            "task": task.model_dump(),
            "_span_collector": span_collector,
        },
        id=str(uuid.uuid4()),
    )
    response = await _dispatcher.dispatch(a2a_request)

    if response.error:
        return {"speech": f"Error: {response.error.message}", "conversation_id": payload.conversation_id}

    result = response.result or {}
    return {
        "speech": result.get("speech", ""),
        "conversation_id": result.get("conversation_id") or payload.conversation_id,
    }


@router.post("/chat/stream")
async def admin_chat_stream(request: Request, payload: ChatRequest):
    """Bridge: session-auth SSE chat -> internal conversation pipeline (streaming)."""
    if _dispatcher is None:
        return JSONResponse(status_code=503, content={"detail": "Dispatcher not ready"})

    # FLOW-MED-9: source is set by TracingMiddleware from the route
    # path.
    span_collector = getattr(request.state, "span_collector", None)
    language = payload.language
    if not language:
        language = await SettingsRepository.get_value("language") or "en"
    prepared_text = prepare_user_text(payload.text)
    task = AgentTask(
        description=prepared_text.text,
        user_text=prepared_text.text,
        conversation_id=payload.conversation_id,
        context=TaskContext(language=language, source="chat", injection_detected=prepared_text.injection_detected),
    )
    a2a_request = JsonRpcRequest(
        method="message/stream",
        params={
            "agent_id": "orchestrator",
            "task": task.model_dump(),
            "_span_collector": span_collector,
        },
        id=str(uuid.uuid4()),
    )

    async def generate():
        root_span_id = getattr(request.state, "root_span_id", None)
        parent_token = None
        if span_collector and root_span_id:
            parent_token = span_collector.push_parent(root_span_id)
        try:
            async for chunk in _dispatcher.dispatch_stream(a2a_request):
                token = StreamToken(
                    token=chunk.result.get("token", ""),
                    done=chunk.done,
                    conversation_id=chunk.result.get("conversation_id") if chunk.done else None,
                    mediated_speech=chunk.result.get("mediated_speech") if chunk.done else None,
                    is_filler=chunk.result.get("is_filler", False),
                    error=chunk.result.get("error") if chunk.done else None,
                )
                yield f"data: {token.model_dump_json()}\n\n"
        finally:
            if span_collector and parent_token is not None:
                span_collector.pop_parent(parent_token)
            if span_collector:
                await span_collector.flush()

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- Send device mappings ---


class SendDeviceMappingCreate(BaseModel):
    display_name: str
    device_type: str
    ha_service_target: str


@router.get("/send-devices")
async def list_send_devices():
    """List all send device mappings."""
    return await SendDeviceMappingRepository.list_all()


@router.post("/send-devices")
async def create_send_device(body: SendDeviceMappingCreate):
    """Create a new send device mapping."""
    if body.device_type not in ("notify", "tts"):
        return JSONResponse({"detail": "device_type must be 'notify' or 'tts'"}, status_code=400)
    if not body.display_name.strip():
        return JSONResponse({"detail": "display_name is required"}, status_code=400)
    if not body.ha_service_target.strip():
        return JSONResponse({"detail": "ha_service_target is required"}, status_code=400)
    existing = await SendDeviceMappingRepository.find_by_name(body.display_name)
    if existing:
        return JSONResponse({"detail": f"Mapping for '{body.display_name}' already exists"}, status_code=409)
    row_id = await SendDeviceMappingRepository.create(
        body.display_name,
        body.device_type,
        body.ha_service_target,
    )
    return {"id": row_id}


@router.put("/send-devices/{mapping_id}")
async def update_send_device(mapping_id: int, body: SendDeviceMappingCreate):
    """Update an existing send device mapping."""
    if body.device_type not in ("notify", "tts"):
        return JSONResponse({"detail": "device_type must be 'notify' or 'tts'"}, status_code=400)
    ok = await SendDeviceMappingRepository.update(
        mapping_id,
        display_name=body.display_name,
        device_type=body.device_type,
        ha_service_target=body.ha_service_target,
    )
    if not ok:
        return JSONResponse({"detail": "Mapping not found"}, status_code=404)
    return {"ok": True}


@router.delete("/send-devices/{mapping_id}")
async def delete_send_device(mapping_id: int):
    """Delete a send device mapping."""
    ok = await SendDeviceMappingRepository.delete(mapping_id)
    if not ok:
        return JSONResponse({"detail": "Mapping not found"}, status_code=404)
    return {"ok": True}


@router.get("/send-devices/available-targets")
async def list_available_send_targets(request: Request, type: str = "notify"):
    """Fetch available HA services/entities for send device mapping."""
    ha_client = request.app.state.ha_client
    if not ha_client:
        return []

    targets = []
    if type == "notify":
        try:
            services = await ha_client.get_services()
            notify_services = services.get("notify", {})
            for svc_name in notify_services:
                targets.append({"id": svc_name, "label": f"notify.{svc_name}"})
        except Exception:
            logger.warning("Failed to fetch notify services from HA", exc_info=True)
    elif type == "tts":
        try:
            states = await ha_client.get_states()
            for state in states:
                eid = state.get("entity_id", "")
                if eid.startswith("media_player."):
                    name = state.get("attributes", {}).get("friendly_name", eid)
                    targets.append({"id": eid, "label": f"{name} ({eid})"})
        except Exception:
            logger.warning("Failed to fetch media_player entities from HA", exc_info=True)

    return targets
