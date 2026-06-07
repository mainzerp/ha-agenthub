"""Trace viewer admin API endpoints."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from app.db.repository import TraceSpanRepository, TraceSummaryRepository
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/traces",
    tags=["admin-traces"],
    dependencies=[Depends(require_admin_session)],
)


# --- Module-level constants for Agent Executions ---

_response_key_map = {
    "return": "final_response",
    "rewrite": "rewritten_text",
    "ha_action": "result_speech",
    "filler_generate": "filler_text",
    "filler_send": "filler_text",
}

_included_span_names = {
    "dispatch",
    "dispatch_content",
    "dispatch_send",
    "classify",
    "llm_provider_call",
    "return",
    "rewrite",
    "ha_action",
    "filler_generate",
    "filler_send",
    "mediation",
    "mcp_tool_call",
    "ha_call",
}


def _build_response(span_name: str, metadata: dict) -> str:
    """Build a human-readable response string for the Agent Executions table."""
    key = _response_key_map.get(span_name)
    if key:
        return str(metadata.get(key, "") or "")

    if span_name == "mediation":
        lang = metadata.get("language", "")
        orig = metadata.get("original_length", "")
        med = metadata.get("mediated_length", "")
        if lang and orig and med:
            return f"Personality rewrite ({lang}), {orig} -> {med} chars"
        if lang:
            return f"Personality rewrite ({lang})"
        return "Personality rewrite"

    if span_name == "mcp_tool_call":
        tool = metadata.get("tool_name", "")
        result = str(metadata.get("result", "") or "")
        if tool and result:
            truncated = result[:120] + "..." if len(result) > 120 else result
            return f"{tool}: {truncated}"
        if tool:
            return tool
        return result

    if span_name == "ha_call":
        service = metadata.get("service", "")
        target = metadata.get("target", "")
        if service and target:
            return f"{service} -> {target}"
        return target or service or ""

    return str(metadata.get("agent_response", "") or "")


# --- Models ---


class LabelUpdate(BaseModel):
    label: str | None = None


# --- Static routes MUST come before /{trace_id} ---


@router.get("/export")
async def export_traces(
    search: str | None = Query(None),
    agent: str | None = Query(None),
    label: str | None = Query(None),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
):
    """Export filtered traces as CSV."""
    rows = await TraceSummaryRepository.export_filtered(
        search=search,
        agent=agent,
        label=label,
        date_from=date_from,
        date_to=date_to,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Timestamp",
            "Trace ID",
            "Conversation ID",
            "User Input",
            "Final Response",
            "Agent",
            "Confidence",
            "Duration (ms)",
            "Label",
            "Source",
            "Agents",
            "Device",
            "Area",
            "Voice Followup",
            "Conversation Turns",
        ]
    )
    for row in rows:
        agents_list = row.get("agents")
        agents_str = ", ".join(agents_list) if isinstance(agents_list, list) else str(agents_list or "")
        device = row.get("device_name") or row.get("device_id") or ""
        area = row.get("area_name") or row.get("area_id") or ""
        voice_followup = "Yes" if row.get("voice_followup") else ""
        conv_turns = row.get("conversation_turns")
        if isinstance(conv_turns, str):
            with contextlib.suppress(json.JSONDecodeError):
                conv_turns = json.loads(conv_turns)
        conv_turns_str = str(len(conv_turns)) if isinstance(conv_turns, list) else str(conv_turns or "")
        writer.writerow(
            [
                row.get("created_at", ""),
                row.get("trace_id", ""),
                row.get("conversation_id", ""),
                row.get("user_input", ""),
                row.get("final_response", ""),
                row.get("routing_agent", ""),
                row.get("routing_confidence", ""),
                row.get("total_duration_ms", ""),
                row.get("label", ""),
                row.get("source", ""),
                agents_str,
                device,
                area,
                voice_followup,
                conv_turns_str,
            ]
        )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=traces_export.csv"},
    )


@router.get("/labels")
async def list_trace_labels():
    """List all distinct labels used on traces."""
    labels = await TraceSummaryRepository.list_labels()
    return {"labels": labels}


# --- Parameterized routes ---


@router.get("")
async def list_traces(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: str | None = Query(None),
    agent: str | None = Query(None),
    label: str | None = Query(None),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
):
    """List recent traces with search, filters, and pagination."""
    traces = await TraceSummaryRepository.list_filtered(
        search=search,
        agent=agent,
        label=label,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )
    total = await TraceSummaryRepository.count_filtered(
        search=search,
        agent=agent,
        label=label,
        date_from=date_from,
        date_to=date_to,
    )
    labels = await TraceSummaryRepository.list_labels()
    agents = await TraceSummaryRepository.list_agents()
    return {
        "traces": traces,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "labels": labels,
        "agents": agents,
    }


@router.get("/{trace_id}")
async def get_trace_detail(trace_id: str):
    """Get detailed trace info including spans and agent executions."""
    summary = await TraceSummaryRepository.get(trace_id)
    if not summary:
        return JSONResponse(status_code=404, content={"detail": "Trace not found"})

    spans = await TraceSpanRepository.get_trace_spans(trace_id)
    spans.sort(key=lambda s: s.get("start_time", ""))

    # Build agent_executions from spans
    agent_executions = []
    for span in spans:
        if span.get("agent_id") and span["span_name"] in _included_span_names:
            meta = span.get("metadata") or {}
            exec_entry = {
                "agent_id": span["agent_id"],
                "span_name": span["span_name"],
                "duration_ms": span["duration_ms"],
                "status": span["status"],
                "response": _build_response(span["span_name"], meta),
                "created_at": span.get("created_at"),
            }
            if meta.get("ttft_ms") is not None:
                exec_entry["ttft_ms"] = meta["ttft_ms"]
            if meta.get("tps") is not None:
                exec_entry["tps"] = meta["tps"]
            agent_executions.append(exec_entry)

    # Build inter-agent communication from spans
    agent_communication = []
    classify_span = None
    dispatch_spans = []
    dispatch_content_spans = []
    dispatch_send_spans = []
    return_span = None
    filler_generate_span = None
    filler_send_span = None
    mcp_tool_spans = []
    routing_cached = False

    for span in spans:
        if span.get("span_name") == "classify":
            classify_span = span
            routing_cached = (span.get("metadata") or {}).get("routing_cached", False)
        elif span.get("span_name") == "dispatch":
            dispatch_spans.append(span)
        elif span.get("span_name") == "dispatch_content":
            dispatch_content_spans.append(span)
        elif span.get("span_name") == "dispatch_send":
            dispatch_send_spans.append(span)
        elif span.get("span_name") == "return":
            return_span = span
        elif span.get("span_name") == "filler_generate":
            filler_generate_span = span
        elif span.get("span_name") == "filler_send":
            filler_send_span = span
        elif span.get("span_name") == "mcp_tool_call":
            mcp_tool_spans.append(span)

    user_input = summary.get("user_input", "")
    final_response = summary.get("final_response", "")

    def _append_tool_calls(agent_id: str = "") -> None:
        for tool_span in mcp_tool_spans:
            if agent_id and tool_span.get("agent_id") != agent_id:
                continue
            tool_meta = tool_span.get("metadata") or {}
            _task = tool_meta.get("arguments") or tool_meta.get("argument_keys", [])
            if isinstance(_task, dict):
                _task = json.dumps(_task, ensure_ascii=False, indent=2)
            agent_communication.append(
                {
                    "from_agent": tool_span.get("agent_id", "agent"),
                    "to_agent": "tool: " + str(tool_meta.get("tool_name", "?")),
                    "task": _task,
                    "response": tool_meta.get("result", ""),
                    "is_tool_call": True,
                    "tool_server": tool_meta.get("server_name", ""),
                    "duration_ms": tool_span.get("duration_ms"),
                }
            )

    # Detect action_cache_hit (no classify span, return span has action_cache_hit
    # or the legacy response_cache_hit key)
    # v3+: structured-key hits also surface as hit_type="action_hit".
    action_cache_hit = False
    if return_span and not classify_span:
        ret_meta = return_span.get("metadata") or {}
        action_cache_hit = bool(ret_meta.get("action_cache_hit") or ret_meta.get("response_cache_hit"))

    if action_cache_hit and return_span:
        # Action cache hit short-circuit
        target = (return_span.get("metadata") or {}).get("from_agent", "")
        agent_communication.append(
            {
                "from_agent": "user",
                "to_agent": "orchestrator",
                "task": user_input,
                "response": "",
                "memory": summary.get("conversation_turns") or [],
            }
        )
        # Find ha_action and rewrite spans for the cached path
        ha_action_span = None
        rewrite_span = None
        for span in spans:
            if span.get("span_name") == "ha_action":
                ha_action_span = span
            elif span.get("span_name") == "rewrite":
                rewrite_span = span
        if ha_action_span:
            ha_meta = ha_action_span.get("metadata") or {}
            agent_communication.append(
                {
                    "from_agent": "orchestrator (cached action)",
                    "to_agent": ha_meta.get("entity", "Home Assistant"),
                    "task": ha_meta.get("action", ""),
                    "response": "success" if ha_meta.get("success") else "failed",
                }
            )
        if rewrite_span:
            rw_meta = rewrite_span.get("metadata") or {}
            agent_communication.append(
                {
                    "from_agent": "rewrite-agent",
                    "to_agent": "orchestrator",
                    "task": rw_meta.get("original_text", ""),
                    "response": rw_meta.get("rewritten_text", ""),
                    "is_rewrite": True,
                }
            )
        agent_communication.append(
            {
                "from_agent": "orchestrator",
                "to_agent": "user",
                "task": "",
                "response": final_response,
                "action_cache_hit": True,
            }
        )
    elif classify_span:
        meta = classify_span.get("metadata") or {}
        target = meta.get("target_agent", "")

        # Step 1: User -> Orchestrator
        agent_communication.append(
            {
                "from_agent": "user",
                "to_agent": "orchestrator",
                "task": user_input,
                "response": "",
                "memory": summary.get("conversation_turns") or [],
            }
        )

        is_sequential_send = len(dispatch_content_spans) > 0 and len(dispatch_send_spans) > 0

        if is_sequential_send:
            # Sequential send path (content agent -> send agent)
            condensed = meta.get("condensed_task", "")

            # Content agent dispatch
            content_ds = dispatch_content_spans[0]
            content_meta = content_ds.get("metadata") or {}
            content_agent = content_meta.get("content_agent") or content_ds.get("agent_id", "")
            content_resp = content_meta.get("agent_response", "")
            if not content_resp:
                for ds in dispatch_spans:
                    if ds.get("agent_id") == content_agent:
                        content_resp = (ds.get("metadata") or {}).get("agent_response", "")
                        break

            step_content = {
                "from_agent": "orchestrator",
                "to_agent": content_agent,
                "task": condensed,
                "response": content_resp,
                "sequential": True,
                "sequential_step": "content",
            }
            if condensed == user_input:
                step_content["task_pass_through"] = True
            agent_communication.append(step_content)
            _append_tool_calls(content_agent)

            # Filler (if present)
            if filler_generate_span:
                fg_meta = filler_generate_span.get("metadata") or {}
                was_sent = fg_meta.get("was_sent", False)
                agent_communication.append(
                    {
                        "from_agent": "filler-agent",
                        "to_agent": "orchestrator",
                        "task": "",
                        "response": fg_meta.get("filler_text", ""),
                        "is_filler": True,
                        "filler_stage": "generated",
                        "filler_was_sent": was_sent,
                    }
                )

            if filler_send_span:
                fs_meta = filler_send_span.get("metadata") or {}
                agent_communication.append(
                    {
                        "from_agent": "orchestrator (filler)",
                        "to_agent": "user",
                        "task": "",
                        "response": fs_meta.get("filler_text", ""),
                        "is_filler": True,
                        "filler_stage": "sent",
                    }
                )

            # Send agent dispatch
            send_ds = dispatch_send_spans[0]
            send_meta = send_ds.get("metadata") or {}
            send_agent = send_ds.get("agent_id", "send-agent")
            send_resp = send_meta.get("agent_response", "")
            if not send_resp:
                for ds in dispatch_spans:
                    if ds.get("agent_id") == send_agent:
                        send_resp = (ds.get("metadata") or {}).get("agent_response", "")
                        break

            agent_communication.append(
                {
                    "from_agent": "orchestrator",
                    "to_agent": send_agent,
                    "task": send_meta.get("send_target", ""),
                    "response": send_resp,
                    "sequential": True,
                    "sequential_step": "send",
                }
            )
            _append_tool_calls(send_agent)

            # Final return
            mediated = False
            if return_span:
                ret_meta = return_span.get("metadata") or {}
                mediated = ret_meta.get("mediated", False)
            agent_communication.append(
                {
                    "from_agent": "orchestrator",
                    "to_agent": "user",
                    "task": "",
                    "response": final_response,
                    "response_unchanged": not mediated,
                }
            )

        elif len(dispatch_spans) <= 1:
            # Single-agent path
            dispatch_span = dispatch_spans[0] if dispatch_spans else None
            agent_resp = ""
            if dispatch_span:
                agent_resp = (dispatch_span.get("metadata") or {}).get("agent_response", "")
            condensed = meta.get("condensed_task", "")

            mediated = False
            if return_span:
                ret_meta = return_span.get("metadata") or {}
                mediated = ret_meta.get("mediated", False)

            if filler_generate_span:
                # With filler: show chronological order
                # 2. Orchestrator dispatches task (no response yet)
                step_dispatch = {
                    "from_agent": "orchestrator",
                    "to_agent": target,
                    "task": condensed,
                    "response": "",
                }
                if condensed == user_input:
                    step_dispatch["task_pass_through"] = True
                agent_communication.append(step_dispatch)

                # 3. Filler generated
                fg_meta = filler_generate_span.get("metadata") or {}
                was_sent = fg_meta.get("was_sent", False)
                agent_communication.append(
                    {
                        "from_agent": "filler-agent",
                        "to_agent": "orchestrator",
                        "task": "",
                        "response": fg_meta.get("filler_text", ""),
                        "is_filler": True,
                        "filler_stage": "generated",
                        "filler_was_sent": was_sent,
                    }
                )

                # 4. Filler sent to user (only if actually sent)
                if filler_send_span:
                    fs_meta = filler_send_span.get("metadata") or {}
                    agent_communication.append(
                        {
                            "from_agent": "orchestrator (filler)",
                            "to_agent": "user",
                            "task": "",
                            "response": fs_meta.get("filler_text", ""),
                            "is_filler": True,
                            "filler_stage": "sent",
                        }
                    )

                _append_tool_calls(target)

                # 5. Agent responds to orchestrator
                agent_communication.append(
                    {
                        "from_agent": target,
                        "to_agent": "orchestrator",
                        "task": "",
                        "response": agent_resp,
                    }
                )

                # 6. Final response (mediated) to user
                agent_communication.append(
                    {
                        "from_agent": "orchestrator",
                        "to_agent": "user",
                        "task": "",
                        "response": final_response,
                        "response_unchanged": (agent_resp == final_response and not mediated),
                    }
                )
            else:
                # Without filler: full 4-step flow matching the filler path
                # Step 2: orchestrator dispatches task to target agent
                step_dispatch = {
                    "from_agent": "orchestrator",
                    "to_agent": target,
                    "task": condensed,
                    "response": "",
                }
                if condensed == user_input:
                    step_dispatch["task_pass_through"] = True
                agent_communication.append(step_dispatch)
                _append_tool_calls(target)

                # Step 3: target agent returns raw response to orchestrator
                agent_communication.append(
                    {
                        "from_agent": target,
                        "to_agent": "orchestrator",
                        "task": "",
                        "response": agent_resp,
                    }
                )

                # Step 4: orchestrator delivers final response to user
                agent_communication.append(
                    {
                        "from_agent": "orchestrator",
                        "to_agent": "user",
                        "task": "",
                        "response": final_response,
                        "response_unchanged": (agent_resp == final_response and not mediated),
                    }
                )
        else:
            # Multi-agent fan-out path
            for ds in dispatch_spans:
                ds_meta = ds.get("metadata") or {}
                ds_agent = ds.get("agent_id", "")
                ds_resp = ds_meta.get("agent_response", "")
                ds_task = ds_meta.get("condensed_task", "")
                agent_communication.append(
                    {
                        "from_agent": "orchestrator",
                        "to_agent": ds_agent,
                        "task": ds_task,
                        "response": ds_resp,
                        "parallel": True,
                    }
                )
            for ds in dispatch_spans:
                _append_tool_calls(ds.get("agent_id", ""))

            # Combined return
            mediated = True  # Multi-agent always has LLM merge
            if return_span:
                ret_meta = return_span.get("metadata") or {}
                mediated = ret_meta.get("mediated", True)
            agent_communication.append(
                {
                    "from_agent": ", ".join(ds.get("agent_id", "") for ds in dispatch_spans),
                    "to_agent": "orchestrator",
                    "task": "",
                    "response": final_response,
                    "response_unchanged": False,
                }
            )

    routing = {
        "selected_agent": summary.get("routing_agent"),
        "confidence": summary.get("routing_confidence"),
        "duration_ms": summary.get("routing_duration_ms"),
        "reasoning": summary.get("routing_reasoning"),
        "action_cache_hit": action_cache_hit,
        "verbatim_terms": summary.get("verbatim_terms"),
    }

    # Enrich with multi-agent routing details from classify span
    if classify_span:
        cls_meta = classify_span.get("metadata") or {}
        if cls_meta.get("multi_agent"):
            routing["multi_agent"] = True
            routing["all_agents"] = cls_meta.get("target_agent", "")
            routing["agent_instructions"] = summary.get("agent_instructions") or {}

    # Compute total duration from spans if not stored
    total_duration_ms = summary.get("total_duration_ms")
    if not total_duration_ms and spans:
        try:
            from datetime import datetime, timedelta

            starts = [datetime.fromisoformat(s["start_time"]) for s in spans if s.get("start_time")]
            if starts:
                min_start = min(starts)
                max_end = max(
                    datetime.fromisoformat(s["start_time"]) + timedelta(milliseconds=s.get("duration_ms", 0))
                    for s in spans
                    if s.get("start_time")
                )
                total_duration_ms = round((max_end - min_start).total_seconds() * 1000, 2)
        except Exception:
            logger.debug("Failed to compute total duration for trace %s", trace_id, exc_info=True)

    return {
        "trace_id": trace_id,
        "conversation_id": summary.get("conversation_id"),
        "timestamp": summary.get("created_at"),
        "duration_ms": total_duration_ms,
        "user_input": summary.get("user_input"),
        "final_response": summary.get("final_response"),
        "routing": routing,
        "agent_instructions": summary.get("agent_instructions"),
        "label": summary.get("label"),
        "source": summary.get("source"),
        # FLOW-CTX-1 (0.18.6): surface the satellite / area that
        # originated the trace so the UI can render them.
        "device_id": summary.get("device_id"),
        "area_id": summary.get("area_id"),
        "device_name": summary.get("device_name"),
        "area_name": summary.get("area_name"),
        "voice_followup": summary.get("voice_followup"),
        "spans": spans,
        "agent_executions": agent_executions,
        "agent_communication": agent_communication,
        "routing_cached": routing_cached,
        "conversation_turns": summary.get("conversation_turns") or [],
    }


@router.put("/{trace_id}/label")
async def update_trace_label(trace_id: str, payload: LabelUpdate):
    """Update or clear the label on a trace."""
    summary = await TraceSummaryRepository.get(trace_id)
    if not summary:
        return JSONResponse(status_code=404, content={"detail": "Trace not found"})
    await TraceSummaryRepository.update_label(trace_id, payload.label)
    return {"status": "ok", "trace_id": trace_id, "label": payload.label}
