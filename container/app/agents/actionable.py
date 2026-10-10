"""Base class for agents that parse LLM output into HA actions.

Also provides the config-driven :class:`_ConfigurableDomainAgent` and
the :class:`DomainAgent` type alias for external imports.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect as _inspect
import logging
import re
import time
from typing import Any

from pydantic import ValidationError

from app.agents.action_executor import (
    VERIFY_UNVERIFIED,
    find_rejected_action_objects,
    parse_actions,
    reset_request_candidate_ids,
    reset_request_visible_entries,
    reset_verify_records,
    set_request_candidate_ids,
    set_request_visible_entries,
    start_verify_records,
    unverified_speech,
)
from app.agents.base import (
    UNTRUSTED_DATA_NOTE,
    UNTRUSTED_NAME_MAX_CHARS,
    UNTRUSTED_STATE_MAX_CHARS,
    UNTRUSTED_TEXT_MAX_CHARS,
    BaseAgent,
    _render_prompt_template,
    language_code_to_name,
    sanitize_untrusted_text,
    wrap_untrusted_data,
)
from app.agents.decorator import agent
from app.analytics.tracer import _optional_span
from app.entity.tokens import entry_field_tokens, normalize_tokenize
from app.entity.visibility import filter_visible_results
from app.models.agent import (
    ActionExecuted,
    AgentCard,
    AgentError,
    AgentErrorCode,
    DispatchTask,
    ExecutedCommand,
    TaskContext,
    TaskResult,
)
from app.security.redaction import redact_sensitive_text, redact_sensitive_values

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```json\s*\n?.*?\n?\s*```", re.DOTALL)
_RAW_JSON_OBJ_RE = re.compile(r'\{[^{}]*"action"\s*:.*?\}', re.DOTALL)

# Per-request state for the currently handled task. Agent instances are
# singletons, so these must be ContextVars: two concurrent requests to the
# same agent would otherwise overwrite each other's context.
_current_task_var: contextvars.ContextVar[DispatchTask | None] = contextvars.ContextVar(
    "actionable_current_task", default=None
)
_current_task_context_var: contextvars.ContextVar[TaskContext | None] = contextvars.ContextVar(
    "actionable_current_task_context", default=None
)


def strip_json_blocks(text: str) -> str:
    """Remove JSON code fences and raw JSON action objects from text."""
    text = _JSON_FENCE_RE.sub("", text)
    text = _RAW_JSON_OBJ_RE.sub("", text)
    return text.strip() or "Sorry, I could not process that request."


# Deterministic localized not-found clarification, used as the fallback
# when the LLM clarifying-question call fails or returns empty (the turn
# must never die on an LLM error). Template dict mirrors the localized
# fallback pattern in ``background_actions.py`` (de/en, English fallback,
# ASCII-only German).
_NOT_FOUND_SPEECH_TEMPLATES = {
    "de": "Ich konnte '{entity}' nicht finden. Welches Geraet meinst du?",
    "en": "I could not find '{entity}'. Which device did you mean?",
}


def _not_found_speech(entity_query: str, language: str | None) -> str:
    """Build the deterministic localized not-found clarification speech."""
    lang = (language or "en").lower().split("-", 1)[0]
    template = _NOT_FOUND_SPEECH_TEMPLATES.get(lang, _NOT_FOUND_SPEECH_TEMPLATES["en"])
    return template.format(entity=entity_query)


def _default_result_speech(result: dict) -> str:
    """Fallback speech when an executor returned no ``speech`` after running."""
    if result.get("success"):
        return "Done."
    return "Sorry, I could not complete that action."


# Deterministic speech when the LLM emitted an action object that failed
# validation (e.g. ``{"action": "turn_on", "entity": null}``). The prose
# around such an object may claim success ("Done, the light is on") although
# nothing executed, so it is never spoken. Same de/en template pattern as
# ``_NOT_FOUND_SPEECH_TEMPLATES``.
_INVALID_ACTION_TARGET_SPEECH = {
    "de": "Entschuldigung, ich konnte nicht erkennen, welches Geraet du meinst. Welches Geraet soll ich verwenden?",
    "en": "Sorry, I could not tell which device you meant. Which device should I use?",
}
_INVALID_ACTION_SPEECH = {
    "de": "Entschuldigung, das konnte ich nicht ausfuehren. Kannst du das anders formulieren?",
    "en": "Sorry, I could not carry that out. Could you rephrase it?",
}


def _invalid_action_speech(rejected: list[dict], language: str | None) -> str:
    """Localized clarification for a rejected (invalid) action object."""
    lang = (language or "en").lower().split("-", 1)[0]
    missing_target = any(
        isinstance(obj.get("action"), str)
        and obj.get("action", "").strip()
        and not str(obj.get("entity") or "").strip()
        and not str(obj.get("entity_id") or "").strip()
        for obj in rejected
    )
    templates = _INVALID_ACTION_TARGET_SPEECH if missing_target else _INVALID_ACTION_SPEECH
    return templates.get(lang, templates["en"])


# ENTITY_RES_REDESIGN Phase 7 (ambiguity follow-up MVP): when the top-1/top-2
# candidate score gap is below this threshold, the candidate block is
# annotated so the agent LLM asks a short clarifying question instead of
# guessing. Module constant on purpose (MVP, no settings seed); the value
# mirrors the area re-rank gap in deterministic_resolver.py.
_AMBIGUITY_SCORE_GAP = 0.05

# English-only per Directive 13 -- the LLM translates its own answer into
# the user's language.
_AMBIGUITY_ANNOTATION = (
    "Note: the top candidates have very close scores, so the request is ambiguous. "
    "Do not guess: ask the user a short clarifying question instead "
    "(for example, 'Did you mean <name 1> or <name 2>?') in the user's language."
)

# Follow-up inversion: on the turn answering a pending clarifying question,
# tied candidates are resolved by the answer instead of re-asking. The
# pending question is interpolated as delimited, length-bounded data.
_CHOOSE_AND_ACT_ANNOTATION = (
    "The user is answering your clarifying question:\n{pending_question}\n"
    "Pick the candidate from the list that matches the answer and execute; "
    "do NOT ask again unless no candidate fits."
)

# Agent-side keyword recall (ENTITY_RESOLUTION_REWORK): domains with at most
# this many visible entities skip filtering -- the whole visible list is
# injected. Larger domains are token-filtered down to the top N.
_KEYWORD_RECALL_MAX_INJECT = 15
_KEYWORD_RECALL_TOP_N = 12
# Compound containment guard, mirroring the matcher's near-miss token-length
# guard: an entity token of at least this length that is a substring of a
# query token counts as a hit ("innenhof" inside "innenhofuberdachung").
_KEYWORD_COMPOUND_MIN_TOKEN_LEN = 4


def _recall_is_ambiguous(
    scored: list[tuple[Any, tuple[int, int, int]]],
    preferred_area_id: str | None = None,
) -> bool:
    """True when the two best recall score tuples tie.

    Scores are per-field-class int hit counts ``(name, identity, area)``;
    with int tuple scores the ``< _AMBIGUITY_SCORE_GAP`` semantics reduce
    to tuple equality, so ``_AMBIGUITY_SCORE_GAP`` is kept for documentation
    (it mirrors the area re-rank gap in deterministic_resolver.py) and no
    fractional scores are introduced.

    Satellite-area tie-break ("here" / "this room"): when exactly one of
    the tied top candidates sits in the satellite's area
    (``preferred_area_id``), the tie is resolved by that area and the
    recall is not ambiguous.
    """
    if len(scored) < 2:
        return False
    hits = sorted((hit_counts for _entry, hit_counts in scored), reverse=True)
    if hits[0] == (0, 0, 0):
        return False
    if hits[0] != hits[1]:
        return False
    if preferred_area_id:
        top = hits[0]
        in_area = [
            entry for entry, hit_counts in scored if hit_counts == top and _entry_area(entry) == preferred_area_id
        ]
        if len(in_area) == 1:
            return False
    return True


def _entry_area(entry: Any) -> str | None:
    return getattr(entry, "area", None) or None


class ActionableAgent(BaseAgent):
    """Base for domain agents that parse actions from LLM output and execute via HA.

    Subclasses must define:
        - agent_card (property)
        - _prompt_name (str): name of the prompt file (e.g., "light")
        - _do_execute(): async method that delegates to the domain-specific executor
    """

    _prompt_name: str = ""
    _clarify_on_not_found: bool = True
    _allowed_domains: frozenset[str] | None = None
    _supports_conditions: bool = False

    def __init__(self, ha_client=None, entity_index=None, entity_matcher=None) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._entity_matcher = entity_matcher

    def _get_current_task(self) -> DispatchTask | None:
        return _current_task_var.get()

    def _get_current_task_context(self) -> TaskContext | None:
        return _current_task_context_var.get()

    async def _recall_keyword_candidates(self, task: DispatchTask) -> list[tuple[Any, tuple[int, int, int]]]:
        """Keyword/token entity recall over the agent's visible entities.

        ENTITY_RESOLUTION_REWORK: agent-side recall replaces both the
        orchestrator's ingress matcher pass and embedding-based entity
        recall. Each visible entry is scored per field class by normalized
        token overlap against the task description plus the last user turn
        (anaphora follow-ups), with compound containment (an entity token >=
        ``_KEYWORD_COMPOUND_MIN_TOKEN_LEN`` chars contained in a query
        token counts as a hit) so German compounds match their parts. The
        score is a ``(name_hits, identity_hits, area_hits)`` tuple so
        name/alias evidence outranks area-token evidence. The name class
        gets a +1 exact-name bonus when every token of the friendly name
        (or of one alias) appears verbatim in the query, so an explicitly
        named entity outranks partial name overlaps ("Küche" beats
        "Küche Steckdose 1" for the query "Küche ausschalten"); identical
        names both receive the bonus and stay tied.

        Small domains (<= ``_KEYWORD_RECALL_MAX_INJECT`` visible entities)
        skip filtering: the whole visible list is returned, sorted by the
        score tuple. Larger domains return the top ``_KEYWORD_RECALL_TOP_N``
        entries with at least one hit in any class (pure area queries must
        stay recallable).

        Returns a list of ``(entry, (name_hits, identity_hits, area_hits))``
        tuples.
        """
        if self._entity_index is None:
            return []
        agent_id = self.agent_card.agent_id
        entries = await self._entity_index.list_entries_async(domains=self._allowed_domains)
        visible = await filter_visible_results(agent_id, entries, self._entity_index)
        if not visible:
            return []
        # Publish the per-request visible-entries snapshot so the post-LLM
        # executor validation reuses it instead of re-listing the index
        # (previously published by _resolve_relevant_entities).
        set_request_visible_entries(self._allowed_domains, agent_id, visible)

        terms = [task.description] if task.description else []
        if task.context and task.context.conversation_turns:
            for turn in reversed(task.context.conversation_turns):
                if turn.get("role") == "user" and turn.get("content"):
                    terms.append(turn["content"])
                    break
        query_tokens: set[str] = set()
        for term in terms:
            query_tokens.update(normalize_tokenize(term))

        def _hits(entry: Any) -> tuple[int, int, int]:
            counts = []
            for class_tokens in entry_field_tokens(entry):
                hits = 0
                for token in class_tokens:
                    if token in query_tokens or (
                        len(token) >= _KEYWORD_COMPOUND_MIN_TOKEN_LEN
                        and any(len(qt) >= _KEYWORD_COMPOUND_MIN_TOKEN_LEN and token in qt for qt in query_tokens)
                    ):
                        hits += 1
                counts.append(hits)
            name_sources = [getattr(entry, "friendly_name", "") or ""]
            name_sources.extend(getattr(entry, "aliases", None) or [])
            if any((tokens := normalize_tokenize(src)) and tokens <= query_tokens for src in name_sources):
                counts[0] += 1
            return counts[0], counts[1], counts[2]

        # Satellite area ("here" / "this room"): entities in the area the
        # user is speaking from win ties and stay recallable in large
        # domains even without a token hit.
        satellite_area_id = task.context.area_id if task.context else None

        def _in_satellite_area(entry: Any) -> bool:
            return bool(satellite_area_id) and _entry_area(entry) == satellite_area_id

        def _sort_key(pair: tuple[Any, tuple[int, int, int]]) -> tuple[int, int, int, int, int]:
            name_hits, identity_hits, area_hits = pair[1]
            # Ties: satellite area first, then shorter name -- the LLM makes the final decision.
            return (
                -name_hits,
                -identity_hits,
                -area_hits,
                0 if _in_satellite_area(pair[0]) else 1,
                len(getattr(pair[0], "friendly_name", "") or ""),
            )

        if len(visible) <= _KEYWORD_RECALL_MAX_INJECT:
            scored = [(entry, _hits(entry)) for entry in visible]
            scored.sort(key=_sort_key)
            return scored

        scored = [(entry, _hits(entry)) for entry in visible]
        scored = [pair for pair in scored if any(pair[1]) or _in_satellite_area(pair[0])]
        scored.sort(key=_sort_key)
        return scored[:_KEYWORD_RECALL_TOP_N]

    async def _build_query_candidate_context(
        self, task: DispatchTask
    ) -> tuple[str | None, list[tuple[Any, tuple[int, int, int]]]]:
        """Build the closed-contract candidate block from keyword recall.

        Returns ``(block, scored_candidates)``. The block lists candidates
        as ``entity_id -- friendly_name (state)``; the LLM must emit the
        ``entity_id`` field verbatim from this list (an id outside the list
        is rejected fail-closed by the executor, without a matcher re-run).
        An empty recall yields a block instructing the LLM to ask which
        device the user means (natural language, no JSON action).
        """
        recalled = await self._recall_keyword_candidates(task)

        if not recalled:
            return (
                "No matching devices were found for this request. "
                "Do NOT output a JSON action block. "
                "Ask the user a short clarifying question in natural language to find out which device they mean.",
                [],
            )

        # Friendly names and states are untrusted (anyone who can rename a
        # device or set a text state controls them): delimited and bounded.
        candidate_lines = []
        for idx, (entry, _hits) in enumerate(recalled, start=1):
            entity_id = getattr(entry, "entity_id", "") or ""
            friendly_name = sanitize_untrusted_text(
                getattr(entry, "friendly_name", "") or entity_id, UNTRUSTED_NAME_MAX_CHARS
            )
            state = sanitize_untrusted_text(getattr(entry, "state", None) or "-", UNTRUSTED_STATE_MAX_CHARS)
            candidate_lines.append(f"{idx}. {entity_id} — {friendly_name} ({state})")
        lines = ["Candidate entities (choose from this list only):", wrap_untrusted_data("\n".join(candidate_lines))]
        lines.append(UNTRUSTED_DATA_NOTE)
        lines.append("")
        lines.append(
            "You MUST emit the 'entity_id' field verbatim from this candidate list in the JSON action block. "
            "An entity_id that is not in this list is rejected and the action will not run. "
            "If none of the candidates fits the user's request, do NOT output a JSON action block: "
            "ask a short clarifying question in natural language instead."
        )
        # Phase 7: ambiguous top-1/top-2 gap -> ask, don't guess. The
        # clarifying question rides the existing voice_followup round-trip;
        # the deterministic *_ambiguous executor speech stays as fallback.
        # Follow-up inversion: when this turn answers a pending clarifying
        # question, tied candidates are resolved by the answer instead of
        # re-asking (composition contract: ambiguity is computed once here).
        context = task.context
        if _recall_is_ambiguous(recalled, context.area_id if context else None):
            if context is not None and context.is_followup and context.pending_question:
                lines.append("")
                pending = wrap_untrusted_data(
                    sanitize_untrusted_text(context.pending_question, UNTRUSTED_TEXT_MAX_CHARS)
                )
                lines.append(_CHOOSE_AND_ACT_ANNOTATION.format(pending_question=pending))
            else:
                lines.append("")
                lines.append(_AMBIGUITY_ANNOTATION)
        return "\n".join(lines), recalled

    def _build_last_entities_context(self, task: DispatchTask) -> str | None:
        """Build a compact anaphora recency-hint block from the task context.

        ENTITY_RES_REDESIGN Phase 6: ``TaskContext.last_entities`` carries
        the entities acted on in recent turns (ids + names, no states),
        most recent first. The block is a HINT for follow-up references
        ("turn it off") only -- an explicitly named device always wins via
        the candidate block and deterministic-first resolution, and any id
        the LLM echoes from this list is re-validated before execution by
        the same fail-closed gate as a candidate pick (domain + index
        existence + visibility, Phase 5).
        """
        context = task.context
        if not context or not context.last_entities:
            return None
        entity_lines = []
        for idx, entry in enumerate(context.last_entities[:3], start=1):
            name = sanitize_untrusted_text(entry.friendly_name or entry.entity_id, UNTRUSTED_NAME_MAX_CHARS)
            entity_lines.append(f"{idx}. {name} ({entry.entity_id})")
        lines = [
            "Recently controlled entities (most recent first; use these ONLY for follow-up references "
            'like "it", "that one", or "the same device"):',
            wrap_untrusted_data("\n".join(entity_lines)),
        ]
        lines.append("")
        lines.append(
            "When the user explicitly names a device, resolve THAT name instead (candidate list above) -- "
            "an explicit mention always wins over recency. You may emit an entity_id from this list in the "
            "'entity_id' field; it will be validated before execution."
        )
        return "\n".join(lines)

    async def _candidate_context_or_none(
        self, task: DispatchTask
    ) -> tuple[str | None, list[tuple[Any, tuple[int, int, int]]] | None]:
        """Failure-contained wrapper around ``_build_query_candidate_context``.

        Returns ``(block, scored_candidates)``; ``(None, None)`` when the
        recall failed -- a failing recall must never abort the turn.
        """
        try:
            return await self._build_query_candidate_context(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Query candidate injection failed for %s", self.agent_card.agent_id, exc_info=True)
            return None, None

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        """Execute the parsed action. Subclasses must override."""
        raise NotImplementedError

    async def _generate_not_found_speech(self, entity_query: str, task: DispatchTask, span_collector=None) -> str:
        """Ask the LLM to generate a language-appropriate clarifying question when an entity is not found.

        Falls back to the deterministic localized template when the LLM
        call fails or returns empty, so the turn never dies on an LLM
        error.
        """
        language = (task.context.language if task.context else None) or "en"
        try:
            # Prompt load inside the try: a cold cache miss is read off the
            # event loop and a missing file degrades to the template.
            system_prompt = _render_prompt_template(
                await self._load_prompt_async("entity_not_found"), language=language_code_to_name(language)
            )
            entity_label = sanitize_untrusted_text(entity_query, UNTRUSTED_NAME_MAX_CHARS)
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"The user asked: {self._wrap_user_input(task.description)}\n"
                        f'No device named "{entity_label}" was found. '
                        "Generate a brief clarifying question asking the user to specify which device they mean."
                    ),
                },
            ]
            result = await self._call_llm(messages, span_collector=span_collector)
            return result.strip() if result and result.strip() else _not_found_speech(entity_query, language)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Not-found clarification LLM call failed", exc_info=True)
            return _not_found_speech(entity_query, language)

    async def _execute_parsed_action(
        self,
        action: dict,
        task: DispatchTask,
        agent_id: str,
        span_collector=None,
    ) -> dict:
        """Execute one parsed action dict inside its own ``ha_action`` span.

        Wraps :meth:`_do_execute` with the same span metadata as the
        former inline path and applies the not-found clarification
        substitution to the executor result. Exceptions propagate -- the
        caller decides whether to abort the turn (single action) or
        degrade to a per-action error result (multi-action turn).
        """
        records_token, verify_records = start_verify_records()
        try:
            if span_collector:
                async with span_collector.start_span("ha_action", agent_id=agent_id) as span:
                    result = await self._do_execute(
                        action,
                        self._ha_client,
                        self._entity_index,
                        self._entity_matcher,
                        agent_id=agent_id,
                        span_collector=span_collector,
                    )
                    result = self._apply_verification_outcome(action, result, verify_records)
                    span["metadata"]["action"] = action.get("action")
                    span["metadata"]["entity"] = action.get("entity")
                    span["metadata"]["success"] = result.get("success")
                    span["metadata"]["action_params"] = redact_sensitive_values(
                        {k: v for k, v in action.items() if k not in ("action", "entity")}
                    )
                    span["metadata"]["result_speech"] = (result.get("speech") or "")[:500]
                    if verify_records:
                        span["metadata"]["verify_outcome"] = verify_records[-1]["outcome"]
            else:
                result = await self._do_execute(
                    action,
                    self._ha_client,
                    self._entity_index,
                    self._entity_matcher,
                    agent_id=agent_id,
                    span_collector=span_collector,
                )
                result = self._apply_verification_outcome(action, result, verify_records)
        finally:
            reset_verify_records(records_token)

        # Entity not found: replace the executor's generic English
        # speech with an LLM-generated clarifying question (with a
        # deterministic localized fallback when the LLM call fails).
        # LOW-15: skip the generic clarification when the resolver already produced a
        # targeted disambiguation speech ("Multiple entities match ..."), signalled by a
        # resolution_path ending in "_ambiguous". Otherwise the deterministic message would
        # be overwritten by a vague "which device did you mean?" question.
        resolution_path = (result.get("metadata") or {}).get("resolution_path") or ""
        if (
            self._clarify_on_not_found
            and not result.get("success")
            and result.get("entity_id") is None
            and not result.get("error")
            and not resolution_path.endswith("_ambiguous")
        ):
            entity_query = action.get("entity", "")
            result = {
                **result,
                "speech": await self._generate_not_found_speech(entity_query, task, span_collector),
            }
        return result

    @staticmethod
    def _apply_verification_outcome(action: dict, result: dict, verify_records: list[dict]) -> dict:
        """Hedge a successful result whose new state was not confirmed.

        ``call_service_with_verification`` records one outcome per service
        call. An ``unverified`` outcome of an action with an expected target
        state (nothing observed, or still the
        pre-call state after the verify window -- common for Zigbee and
        cloud devices that report late) is not a failure, but the plain
        confirmation must not be spoken and the result must not enter the
        action cache.
        """
        if not result.get("success") or not verify_records:
            return result
        entity_id = result.get("entity_id")
        relevant = [rec for rec in verify_records if not entity_id or rec.get("entity_id") == entity_id]
        last = relevant[-1] if relevant else None
        # Only actions with a deterministic target are hedged: toggles and
        # attribute-only actions (fan speed, volume) often change no state.
        if last is None or last.get("outcome") != VERIFY_UNVERIFIED or not last.get("expected_state"):
            return result
        metadata = dict(result.get("metadata") or {})
        friendly_name = metadata.get("top_friendly_name") or action.get("entity") or entity_id or "the device"
        metadata["verify_outcome"] = VERIFY_UNVERIFIED
        return {
            **result,
            "speech": unverified_speech(str(friendly_name)),
            "cacheable": False,
            "metadata": metadata,
        }

    @staticmethod
    def _action_executed_from_result(
        action: dict,
        result: dict,
        entity_ids: list[str] | None = None,
        multi_action: bool = False,
    ) -> ActionExecuted:
        """Build the :class:`ActionExecuted` model for one executor result.

        P1-5: forwards the action's structured parameters (brightness,
        color_temp, transition, ...) as ``service_data`` so the
        orchestrator can persist them on the response cache entry and
        replay the exact same call on the next hit. Executors may
        optionally override this by returning ``service_data`` on the
        result dict.
        """
        return ActionExecuted(
            action=action.get("action", ""),
            entity_id=result.get("entity_id") or "",
            success=result.get("success", False),
            new_state=result.get("new_state"),
            cacheable=result.get("cacheable", True),
            noop=result.get("noop", False),
            service_data=(
                result.get("service_data")
                if isinstance(result.get("service_data"), dict)
                else (action.get("parameters") or {})
            ),
            executed_command=(
                ExecutedCommand.model_validate(result["executed_command"])
                if isinstance(result.get("executed_command"), dict)
                else None
            ),
            entity_ids=entity_ids or [],
            multi_action=multi_action,
        )

    @staticmethod
    def _result_requests_voice_followup(result: dict) -> bool:
        """FOLLOW_UP_QUESTION heuristic for a single executor result.

        True when the result requests a voice follow-up: either the
        executor set the flag itself, or the result is a not-found
        clarifying question -- the LLM/deterministic localized
        clarification (speech ends with "?") or the targeted
        deterministic disambiguation (``_ambiguous``, no "?").
        """
        resolution_path = (result.get("metadata") or {}).get("resolution_path") or ""
        is_ambiguous = resolution_path.endswith("_ambiguous")
        is_not_found = not result.get("success") and result.get("entity_id") is None and not result.get("error")
        return bool(result.get("voice_followup")) or (
            is_not_found and (is_ambiguous or (result.get("speech") or "").rstrip().endswith("?"))
        )

    async def _handle_multi_action(
        self,
        actions: list[dict],
        task: DispatchTask,
        agent_id: str,
        span_collector,
        timing_marks: tuple[float, float, float, float],
    ) -> TaskResult:
        """Execute several parsed action blocks sequentially and merge the results.

        Multi-action turns (the LLM emitted one fenced JSON block per
        requested action) run in utterance order -- never parallelized --
        so per-action ``ha_action`` spans stay ordered. A raising action
        degrades to a per-action error result and the loop continues with
        the next action. ``timing_marks`` carries the ``_handle_task_inner``
        marks ``(t0, t1, t2, t3)`` for the dispatch-timing log line.
        """
        _t0, _t1, _t2, _t3 = timing_marks
        logger.info("multi_action agent=%s actions=%d", agent_id, len(actions))
        results: list[dict] = []
        for action in actions:
            try:
                results.append(await self._execute_parsed_action(action, task, agent_id, span_collector))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Action execution failed for %s action=%s", agent_id, redact_sensitive_values(action))
                entity = action.get("entity", "the device")
                results.append(
                    {
                        "success": False,
                        "entity_id": None,
                        "new_state": None,
                        "speech": f"Sorry, I could not execute the action on {entity}.",
                        # Carries an error so a turn where EVERY action
                        # raised still surfaces ACTION_FAILED like the
                        # single-action path does.
                        "error": {
                            "code": AgentErrorCode.ACTION_FAILED,
                            "message": f"Could not execute the action on {entity}.",
                            "recoverable": True,
                        },
                    }
                )
        _t4 = time.perf_counter()

        # Rare edge: a directive result (e.g. a timer-native delegation)
        # cannot be merged into a combined action result -- surface it alone.
        for result in results:
            if result.get("directive"):
                logger.warning("multi_action: directive result in multi-action turn for %s", agent_id)
                return TaskResult(
                    speech=result.get("speech", ""),
                    directive=result.get("directive"),
                    reason=result.get("reason"),
                    metadata=result.get("metadata") or {},
                    voice_followup=bool(result.get("voice_followup")),
                )

        # Turn-level error only when EVERY action failed with an executor
        # error that validates as AgentError. Partial failures are not
        # turn errors (same philosophy as multi-agent dispatch).
        executor_errors: list[AgentError] = []
        for result in results:
            raw_error = result.get("error")
            if raw_error is None:
                continue
            try:
                executor_errors.append(
                    raw_error if isinstance(raw_error, AgentError) else AgentError.model_validate(raw_error)
                )
            except ValidationError:
                logger.debug("multi_action: result error failed AgentError validation", exc_info=True)
        turn_error = executor_errors[0] if len(executor_errors) == len(results) else None

        # Anaphora/cache surface: the ids every action resolved to
        # (successful results only, deduped in execution order).
        entity_ids = list(
            dict.fromkeys(
                str(result["entity_id"]) for result in results if result.get("success") and result.get("entity_id")
            )
        )

        metadata = results[0].get("metadata") or {}
        _t5 = time.perf_counter()
        logger.info(
            "dispatch_timing agent=%s pre_entities=%.1fms entities=%.1fms llm_parse=%.1fms ha_action=%.1fms post_action=%.1fms total=%.1fms",
            agent_id,
            (_t1 - _t0) * 1000,
            (_t2 - _t1) * 1000,
            (_t3 - _t2) * 1000,
            (_t4 - _t3) * 1000,
            (_t5 - _t4) * 1000,
            (_t5 - _t0) * 1000,
        )
        return TaskResult(
            # Merge per-action confirmations into one utterance.
            speech=" ".join(s for result in results if (s := (result.get("speech") or "").strip())),
            metadata=metadata,
            voice_followup=any(self._result_requests_voice_followup(result) for result in results),
            error=turn_error,
            # Headline action result mirrors the single-action path: the
            # FIRST result's fields, plus the full acted-on id list. The
            # explicit ``multi_action`` flag keeps the turn out of the
            # action cache even when every block hit the same entity or
            # only one block succeeded (``entity_ids`` would hold one id).
            action_executed=self._action_executed_from_result(
                actions[0], results[0], entity_ids=entity_ids, multi_action=len(actions) > 1
            ),
            actions_executed=[self._action_executed_from_result(a, r) for a, r in zip(actions, results, strict=True)],
        )

    def _handle_parse_miss(self, task: DispatchTask, response: str) -> TaskResult:
        """Return the fallback result when the LLM response has no valid action.

        FOLLOW_UP_QUESTION: when the stripped fallback speech is a
        clarifying question (right-trimmed text ends with ``?``), request a
        voice follow-up so the user's answer is re-dispatched -- the same
        spoken-question heuristic as cache safeguard R-A. Normal prose
        answers keep ``voice_followup=False``.
        """
        rejected = find_rejected_action_objects(response)
        if rejected:
            # The LLM tried to act but its action object failed validation
            # (e.g. ``"entity": null``). Nothing executed, so the prose around
            # it -- which may claim success -- must not be spoken.
            logger.warning(
                "Rejected %d invalid action object(s) for %s: %s",
                len(rejected),
                self.agent_card.agent_id,
                redact_sensitive_values(rejected),
            )
            language = task.context.language if task.context else None
            return TaskResult(
                speech=_invalid_action_speech(rejected, language),
                voice_followup=True,
                metadata={"parse_miss": "invalid_action"},
            )
        speech = strip_json_blocks(response)
        followup = speech.rstrip().endswith("?")
        return TaskResult(speech=speech, voice_followup=followup)

    async def handle_task(self, task: DispatchTask) -> TaskResult:
        # FLOW-CTX-1 (0.18.6): expose the incoming TaskContext so
        # domain-specific ``_do_execute`` implementations can pick up
        # satellite area, device_id and request source without
        # plumbing an extra kwarg through every executor signature.
        # Stored in ContextVars (reset in ``finally``) so concurrent
        # requests on the same singleton agent instance stay isolated.
        context_token = _current_task_context_var.set(task.context)
        task_token = _current_task_var.set(task)
        # P2 resolver efficiency: clear any inherited visible-entries
        # snapshot; the pre-LLM keyword recall publishes a fresh one
        # for the post-LLM ``resolve_and_validate_entity`` pass-through.
        visible_entries_token = set_request_visible_entries(None, None, None)
        # ENTITY_RESOLUTION_REWORK: clear any inherited candidate-id gate;
        # the pre-LLM keyword recall publishes fresh values for the
        # executor's closed-contract validation.
        candidate_ids_token = set_request_candidate_ids(None)
        try:
            try:
                return await self._handle_task_inner(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled failure in %s", self.agent_card.agent_id)
                return self._error_result(
                    AgentErrorCode.INTERNAL,
                    "Sorry, something went wrong while handling that request.",
                )
        finally:
            reset_request_candidate_ids(candidate_ids_token)
            reset_request_visible_entries(visible_entries_token)
            _current_task_var.reset(task_token)
            _current_task_context_var.reset(context_token)

    async def _handle_task_inner(self, task: DispatchTask) -> TaskResult:
        _t0 = time.perf_counter()
        # Pre-initialize the dispatch-timing marks: a failing candidate
        # recall degrades fail-soft and must not break the timing log.
        _t1 = _t2 = _t0
        agent_id = self.agent_card.agent_id
        span_collector = task.span_collector
        system_prompt = await self._load_prompt_async(self._prompt_name)

        # Inject language directive for non-English users AFTER the static
        # prompt body (P3 prompt-prefix hygiene; previously PREPENDED). The
        # static head -- including the English-only few-shot examples
        # (Directive 13) -- stays byte-stable across languages so provider
        # prompt-prefix caching can hit it. Placing the directive
        # immediately after the examples also keeps it closer to the
        # generation point (recency), which counters example language
        # bleed; the directive text itself is unchanged.
        language = None
        if task.context:
            language = task.context.language
        if language and language.lower() not in ("en", "english", ""):
            lang_directive = (
                f"CRITICAL LANGUAGE INSTRUCTION: The user's language is {language}.\n"
                f"Respond in {language}.\n"
                f"Copy entity, device, room, and scene names verbatim from the user's message.\n"
                f"NEVER translate entity names to English, regardless of what language the few-shot examples use.\n"
                f"If a few-shot example uses a different language than the user, copy the example's STRUCTURE but keep the USER's original entity names unchanged."
            )
            system_prompt = f"{system_prompt}\n\n{lang_directive}"

        # Inject time/location context (append: data, not constraint rule)
        time_location = self._build_time_location_context(task.context)
        if time_location:
            system_prompt += f"\n\n{time_location}"

        # Generic state-aware output rules (Phase 3). The conditional block is
        # appended only for agents whose executor evaluates the condition field.
        system_prompt += (
            "\n\nOutput rules:\n"
            "- ALWAYS output a JSON action block when an action is determinable; otherwise respond with natural text only.\n"
            "- Execute the action the user explicitly requested, using only the actions documented in your prompt above.\n"
            "- The injected entity states above are for context only. Do NOT describe them in your response.\n"
            '- Only use toggle when the user explicitly says "toggle".'
        )
        if self._supports_conditions:
            system_prompt += (
                "\n\nConditional actions:\n"
                '- When the user says "if X, then Y", use the optional "condition" field.\n'
                "- The condition references another entity by name and an expected state.\n"
                '- Example JSON: {"action": "turn_on", "entity": "Keller", "condition": {"entity": "outdoor brightness", "state": "dark"}}'
            )

        # Inject the keyword-recall candidate block (closed contract). The
        # candidate block already carries entity states -- no separate
        # resolve/state-context pass runs before the LLM. The recall runs
        # in the parent context so the visible-entries snapshot ContextVar
        # it publishes propagates to the post-LLM executor validation.
        candidate_context: str | None = None
        recalled: list[tuple[Any, tuple[int, int, int]]] | None = None
        try:
            async with _optional_span(span_collector, "entity_resolution", agent_id=agent_id) as er_span:
                _t1 = time.perf_counter()
                candidate_context, recalled = await self._candidate_context_or_none(task)
                _t2 = time.perf_counter()
                er_span["metadata"]["recall_ms"] = round((_t2 - _t1) * 1000, 1)
                # Keyword recall (closed contract): surface the candidate
                # pool in the trace so recall gaps are debuggable.
                scored_recall = recalled or []
                er_span["metadata"]["recall_count"] = len(scored_recall)
                er_span["metadata"]["recall_candidates"] = [
                    {
                        "entity_id": getattr(entry, "entity_id", "") or "",
                        "hits": {"name": hits[0], "identity": hits[1], "area": hits[2]},
                    }
                    for entry, hits in scored_recall
                ]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Candidate recall failed for %s", agent_id, exc_info=True)
            candidate_context = None
            recalled = None

        if recalled is not None:
            # Publish the closed-contract gate for the post-LLM executor
            # validation: recalled candidate ids plus the last_entities ids
            # (anaphora hints stay validatable). Published here in the
            # parent context because a ContextVar set inside a gather child
            # would not propagate back.
            candidate_ids = {getattr(entry, "entity_id", "") or "" for entry, _hits in recalled}
            candidate_ids.discard("")
            if task.context:
                candidate_ids.update(le.entity_id for le in task.context.last_entities if le.entity_id)
            set_request_candidate_ids(candidate_ids)

        if candidate_context:
            system_prompt += f"\n\n{candidate_context}"

        # ENTITY_RES_REDESIGN Phase 6: anaphora recency hints render next to
        # the candidate block.
        last_entities_context = self._build_last_entities_context(task)
        if last_entities_context:
            system_prompt += f"\n\n{last_entities_context}"

        messages = [{"role": "system", "content": system_prompt}]

        if task.context and task.context.conversation_turns:
            self._append_conversation_turn_messages(messages, task.context.conversation_turns)

        # The orchestrator condenses the user's request into a task written in
        # the user's own language. Agents receive only the distilled description,
        # not the raw user_text.
        user_content = self._wrap_user_input(task.description)

        messages.append({"role": "user", "content": user_content})

        # No LLM timeout retry for domain agents: a retried call (timeout +
        # backoff + full second attempt) plus the HA action can outlive the
        # A2A dispatch timeout, so the orchestrator would fall back after
        # the action already ran.
        try:
            if span_collector:
                async with span_collector.start_span("llm_call", agent_id=agent_id) as span:
                    response = await self._call_llm(messages, span_collector=span_collector, retry_on_timeout=False)
                    span["metadata"]["model"] = agent_id
                    span["metadata"]["llm_response"] = redact_sensitive_text(response[:500] if response else "")
            else:
                response = await self._call_llm(messages, retry_on_timeout=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("LLM call failed for %s: %s", agent_id, str(e)[:200])
            return self._error_result(
                AgentErrorCode.LLM_ERROR,
                "The language model could not complete this request. Please try again.",
            )

        _t3 = time.perf_counter()

        if not response:
            logger.warning("LLM returned empty response for %s task: %s", agent_id, task.description[:100])
            return self._error_result(
                AgentErrorCode.LLM_EMPTY_RESPONSE,
                "The language model did not return a response. Please try again.",
            )

        actions = parse_actions(response)

        # Path A: Action(s) + HA client -> execute
        if actions and self._ha_client:
            # Multi-action turn: the LLM emitted several fenced action
            # blocks. They run sequentially in utterance order, each with
            # its own ha_action span, and their results are merged.
            if len(actions) > 1:
                return await self._handle_multi_action(actions, task, agent_id, span_collector, (_t0, _t1, _t2, _t3))
            action = actions[0]
            try:
                result = await self._execute_parsed_action(action, task, agent_id, span_collector)

                _t4 = time.perf_counter()

                # FOLLOW_UP_QUESTION: every clarifying question produced on a
                # not-found result -- the LLM/deterministic localized
                # clarification (speech ends with "?") or the targeted
                # deterministic disambiguation ("_ambiguous", no "?") --
                # requests a voice follow-up. When _clarify_on_not_found is
                # False the plain English statement has no trailing "?" and
                # is_ambiguous is False, so the flag stays False.
                resolution_path = (result.get("metadata") or {}).get("resolution_path") or ""
                is_ambiguous = resolution_path.endswith("_ambiguous")
                final_speech = result.get("speech") or ""
                is_not_found = not result.get("success") and result.get("entity_id") is None and not result.get("error")
                followup_question = is_not_found and (is_ambiguous or final_speech.rstrip().endswith("?"))

                metadata = result.get("metadata") or {}
                _t5 = time.perf_counter()
                logger.info(
                    "dispatch_timing agent=%s pre_entities=%.1fms entities=%.1fms llm_parse=%.1fms ha_action=%.1fms post_action=%.1fms total=%.1fms",
                    agent_id,
                    (_t1 - _t0) * 1000,
                    (_t2 - _t1) * 1000,
                    (_t3 - _t2) * 1000,
                    (_t4 - _t3) * 1000,
                    (_t5 - _t4) * 1000,
                    (_t5 - _t0) * 1000,
                )
                if result.get("directive"):
                    return TaskResult(
                        speech=result.get("speech", ""),
                        directive=result.get("directive"),
                        reason=result.get("reason"),
                        metadata=metadata,
                        voice_followup=bool(result.get("voice_followup")) or followup_question,
                    )
                explicit_error = result.get("error")
                if explicit_error:
                    error = explicit_error
                    if not isinstance(error, AgentError):
                        error = AgentError.model_validate(explicit_error)
                    return TaskResult(
                        speech=result.get("speech", ""),
                        error=error,
                        metadata=metadata,
                        voice_followup=bool(result.get("voice_followup")) or followup_question,
                    )
                return TaskResult(
                    speech=final_speech or _default_result_speech(result),
                    metadata=metadata,
                    voice_followup=bool(result.get("voice_followup")) or followup_question,
                    action_executed=self._action_executed_from_result(action, result),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Action execution failed for %s action=%s", agent_id, redact_sensitive_values(action))
                entity = action.get("entity", "the device")
                return self._error_result(
                    AgentErrorCode.ACTION_FAILED,
                    f"Sorry, I could not execute the action on {entity}.",
                )

        # Path B: Action but no HA client
        if actions and not self._ha_client:
            first_action = actions[0]
            logger.warning(
                "Action parsed but ha_client is None for %s: %s", agent_id, redact_sensitive_values(first_action)
            )
            entity = first_action.get("entity", "the device")
            return self._error_result(
                AgentErrorCode.HA_UNAVAILABLE,
                f"I understood the request for {entity}, but the smart home connection is currently unavailable.",
                recoverable=False,
            )

        # Path C: No action (informational)
        return self._handle_parse_miss(task, response)


# ---------------------------------------------------------------------------
# Config-driven domain agent infrastructure (Part 2B)
# ---------------------------------------------------------------------------


class _ConfigurableDomainAgent(ActionableAgent):
    """Domain agent whose behaviour is driven by the @agent decorator metadata.

    Standard agents (light, climate, cover, vacuum, scene, security, media,
    music, automation) are instantiated through this class.  Agents that
    need unique logic (TimerAgent, ListsAgent, CalendarAgent) continue to
    use their own subclasses.
    """

    def __init__(self, ha_client=None, entity_index=None, entity_matcher=None) -> None:
        meta = getattr(self.__class__, "_agent_meta", {})
        self._prompt_name = meta.get("prompt_name", "")
        self._allowed_domains = meta.get("allowed_domains")
        super().__init__(ha_client=ha_client, entity_index=entity_index, entity_matcher=entity_matcher)

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        ctx = self._get_current_task_context()
        area_id = ctx.area_id if ctx else None

        kwargs: dict[str, Any] = {
            "preferred_area_id": area_id,
            "task_context": ctx,
        }

        meta = getattr(self.__class__, "_agent_meta", {})
        executor_module = meta.get("executor_module", "")
        executor_name = meta.get("executor_name", "")

        import importlib as _importlib

        t0 = time.perf_counter()
        executor_fn = getattr(_importlib.import_module(executor_module), executor_name)
        t1 = time.perf_counter()
        sig = _inspect.signature(executor_fn)
        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        t2 = time.perf_counter()
        result = await executor_fn(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            span_collector=span_collector,
            **filtered,
        )
        t3 = time.perf_counter()
        logger.debug(
            "_do_execute %s: import=%.1fms prep=%.1fms exec=%.1fms total=%.1fms",
            agent_id,
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            (t3 - t2) * 1000,
            (t3 - t0) * 1000,
        )
        return result

    @property
    def agent_card(self) -> AgentCard:
        meta = getattr(self.__class__, "_agent_meta", {})
        card_kwargs: dict[str, Any] = {
            "agent_id": meta.get("agent_id", ""),
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "skills": meta.get("skills", []),
            "endpoint": meta.get("endpoint", ""),
        }
        expected_latency = meta.get("expected_latency")
        if expected_latency:
            card_kwargs["expected_latency"] = expected_latency
        timeout_sec = meta.get("timeout_sec")
        if timeout_sec is not None:
            card_kwargs["timeout_sec"] = timeout_sec
        return AgentCard(**card_kwargs)


# -- Domain agent classes (decorated, registered via @agent) ------------------


@agent(
    agent_id="light-agent",
    name="Light Agent",
    description=(
        "Controls and queries lights, switches, and illuminance sensors: on/off, toggle, "
        "brightness, color, color temperature. Reports light/switch status and light-level "
        "readings. Lists all lights and switches. Reads Home Assistant Recorder history for "
        "lights, switches, and illuminance sensors (e.g. how long a light was on yesterday)."
    ),
    skills=[
        "light_control",
        "switch_control",
        "brightness",
        "color",
        "toggle",
        "illuminance_sensor",
        "light_status",
        "light_query",
        "switch_status",
        "switch_query",
        "entity_history",
        "recorder_history",
    ],
    prompt_name="light",
    allowed_domains=frozenset({"light", "switch", "sensor"}),
    executor_module="app.agents.light_executor",
    executor_name="execute_light_action",
)
class LightAgent(_ConfigurableDomainAgent):
    _supports_conditions = True


@agent(
    agent_id="climate-agent",
    name="Climate Agent",
    description=(
        "Controls and queries climate/HVAC devices, fans, humidifiers, environmental sensors, "
        "and local weather conditions/forecasts. Set temperature, HVAC mode, fan speed, "
        "humidity, turn on/off. Control fans: speed, preset mode, oscillation, direction. "
        "Control humidifiers: target humidity, mode. Reads sensors: temperature, humidity, "
        "pressure, dew point, wind, precipitation. Queries weather entities for current "
        "conditions and forecasts."
    ),
    skills=[
        "temperature",
        "hvac_mode",
        "fan_speed",
        "humidity",
        "climate_on_off",
        "sensor_reading",
        "climate_status",
        "sensor_query",
        "weather_sensor",
        "current_weather",
        "weather_forecast",
        "entity_history",
        "recorder_history",
        "fan_control",
        "fan_speed",
        "fan_preset",
        "fan_oscillate",
        "fan_direction",
        "humidifier_control",
        "humidifier_humidity",
        "humidifier_mode",
    ],
    prompt_name="climate",
    allowed_domains=frozenset({"climate", "weather", "sensor"}),
    executor_module="app.agents.climate_executor",
    executor_name="execute_climate_action",
    db_gated=True,
)
class ClimateAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="cover-agent",
    name="Cover Agent",
    description=(
        "Controls and queries covers, blinds, curtains, shutters, garage doors, gates, "
        "awnings, and windows: open, close, stop, set position, and tilt control. "
        "Reports cover status including current position and tilt position. "
        "Lists all cover entities."
    ),
    skills=[
        "cover_control",
        "open",
        "close",
        "stop",
        "set_position",
        "tilt_control",
        "query_cover_state",
        "list_covers",
        "entity_history",
        "recorder_history",
    ],
    prompt_name="cover",
    allowed_domains=frozenset({"cover"}),
    executor_module="app.agents.cover_executor",
    executor_name="execute_cover_action",
)
class CoverAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="vacuum-agent",
    name="Vacuum Agent",
    description=(
        "Controls and queries robot vacuum cleaners: start cleaning, pause, stop, "
        "return to base, clean spot, locate, and set fan speed. Reports vacuum state "
        "including battery level, fan speed, and status. Lists all vacuum entities."
    ),
    skills=[
        "vacuum_control",
        "start",
        "pause",
        "stop",
        "return_to_base",
        "clean_spot",
        "set_fan_speed",
        "locate",
        "query_vacuum_state",
        "list_vacuums",
    ],
    prompt_name="vacuum",
    allowed_domains=frozenset({"vacuum"}),
    executor_module="app.agents.vacuum_executor",
    executor_name="execute_vacuum_action",
)
class VacuumAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="scene-agent",
    name="Scene Agent",
    description=(
        "Activates Home Assistant scenes with optional transition timing. "
        "Lists available scenes and checks if a scene exists."
    ),
    skills=["scene_activate", "scene_list", "scene_query"],
    prompt_name="scene",
    allowed_domains=frozenset({"scene"}),
    executor_module="app.agents.scene_executor",
    executor_name="execute_scene_action",
    db_gated=True,
)
class SceneAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="security-agent",
    name="Security Agent",
    description=(
        "Controls and queries locks, alarm panels, cameras, and security sensors "
        "(motion, door, window, doorbell, smoke, gas). Lock/unlock, arm/disarm, "
        "camera on/off. Reports status and lists all security devices. Reads Home "
        "Assistant Recorder history for those entities (e.g. door open events yesterday)."
    ),
    skills=[
        "lock_control",
        "alarm_control",
        "camera_control",
        "door_sensor",
        "window_sensor",
        "motion_sensor",
        "doorbell",
        "smoke_sensor",
        "security_status",
        "security_query",
        "entity_history",
        "recorder_history",
    ],
    prompt_name="security",
    allowed_domains=frozenset({"lock", "binary_sensor", "alarm_control_panel"}),
    executor_module="app.agents.security_executor",
    executor_name="execute_security_action",
    db_gated=True,
)
class SecurityAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="media-agent",
    name="Media Agent",
    description=(
        "Controls generic media players (TV, Chromecast, streaming devices): "
        "on/off, play/pause/stop, volume, mute, input/source selection. "
        "Reports playback status. Not for music library/Music Assistant -- use music-agent."
    ),
    skills=[
        "tv_control",
        "speaker_control",
        "casting",
        "playback",
        "volume_control",
        "mute",
        "source_selection",
        "media_status",
        "playback_query",
    ],
    prompt_name="media",
    allowed_domains=frozenset({"media_player"}),
    executor_module="app.agents.media_executor",
    executor_name="execute_media_action",
    db_gated=True,
)
class MediaAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="music-agent",
    name="Music Agent",
    description=(
        "Controls music playback via Music Assistant: play, pause, skip, volume, "
        "shuffle, repeat, library search, queue management, playlist/artist/album "
        "selection. Reports current track info and lists music players."
    ),
    skills=[
        "music_playback",
        "volume_control",
        "playlist_selection",
        "library_search",
        "queue_management",
        "shuffle",
        "repeat",
        "music_status",
        "playback_query",
    ],
    prompt_name="music",
    allowed_domains=frozenset({"media_player"}),
    executor_module="app.agents.music_executor",
    executor_name="execute_music_action",
)
class MusicAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="automation-agent",
    name="Automation Agent",
    description=(
        "Enables, disables, triggers, creates, updates, deletes, and queries "
        "Home Assistant automations. Reports status (enabled/disabled, last triggered time). "
        "Lists all automations."
    ),
    skills=[
        "automation_enable",
        "automation_disable",
        "automation_trigger",
        "automation_status",
        "automation_query",
        "automation_create",
        "automation_update",
        "automation_delete",
        "automation_config",
    ],
    prompt_name="automation",
    allowed_domains=frozenset({"automation", "script"}),
    executor_module="app.agents.automation_executor",
    executor_name="execute_automation_action",
    db_gated=True,
)
class AutomationAgent(_ConfigurableDomainAgent):
    pass


DomainAgent = _ConfigurableDomainAgent
