---
applyTo: "**"
---

# Prime Directives

These are project-critical rules for HA-AgentHub. They describe constraints that, if violated, will break the system's correctness, reliability, or architecture. Always verify new code and changes against these before implementing.

---

## 1. Container is the Execution Engine

All orchestration, entity resolution, LLM calls, Home Assistant action execution, caching, and agent dispatch happen inside the container. The Home Assistant integration is an I/O bridge only — it forwards turns and streams responses. It must never resolve entities, classify intent, or call Home Assistant services on behalf of agents.

## 2. Action Cache Hits Require a Visibility Recheck

An action-cache hit must never replay a cached Home Assistant action without first rechecking entity visibility for that turn. If the recheck fails, the entry is invalidated and the turn falls through to live orchestration. Skipping the recheck is a correctness violation.

## 3. Agents Return Executed Results, Not Tool-Call Plans

Domain agents (light-agent, climate-agent, etc.) are responsible for resolving the target entity, calling the relevant Home Assistant service from the container, and returning a verified execution result. Returning a speculative tool plan or a placeholder response without executing the action is not acceptable.

## 4. Entity Resolution: Deterministic-First, Verbatim-Preserving, Visibility-Always

Entity resolution for any executor that targets a Home Assistant entity for a write action must follow this ordered pipeline. Read and history paths should follow the same order whenever the necessary parameters are available.

1. **Verbatim terms first.** The user's literal terms (`entity_id` strings, friendly names, and any explicitly preserved verbatim spans) must be tried before any translated, condensed, summarized, or paraphrased text. Hybrid scoring may not override a verbatim match for the same query.
2. **Exact `entity_id` lookup.** If the query, or any verbatim term, matches an `entity_id` exactly, that entity is selected.
3. **Exact `friendly_name` lookup.** Otherwise, the resolver lists visible candidates within the executor's allowed domains and selects the entity whose normalized `friendly_name` equals the normalized query, or any verbatim term.
4. **Exact normalized alias lookup.** Otherwise, the resolver matches against `aliases` using the same normalization.
5. **Hybrid matcher fallback.** Only if no exact deterministic candidate is found may the executor fall back to the hybrid matcher (fuzzy, embedding, phonetic, area-aware scoring). The fallback must pass `verbatim_terms` and `preferred_domains` derived from the executor's allowed domains.
6. **Visibility on every stage.** The shared visibility filter (see Directive 5) must be applied during exact `entity_id` lookup, exact `friendly_name` and alias sweeps, hybrid scoring, and cached-action replay. Hybrid matching, raw index searches, and any deterministic stage may never surface entities the agent is not allowed to see.
7. **Shared helper required.** Executors must use the shared deterministic-first resolver (`app/entity/deterministic_resolver.py`) rather than calling the hybrid matcher directly. Domain-specific extras, for example light's trailing-device-noun strip and exact-area fallback, may be layered as thin wrappers around the shared helper.
8. **No raw index shortcuts.** Direct `entity_index.search_async(...)` calls in executor code must route through the shared helper or, if a raw index call is unavoidable, must apply the same visibility and allowed-domain filters before selecting a candidate.

## 5. Visibility Rules Are Applied on Every Resolution Path

Entity visibility rules (domain, area, entity, device-class filters) must be applied during deterministic lookup, hybrid matching, and cached-action visibility rechecks. Bypassing visibility at any stage leaks entities that should not be accessible to a given agent.

## 6. A2A Protocol Boundary Is Not Bypassed

Agent-to-agent communication goes through A2A message envelopes shaped around JSON-RPC 2.0 semantics. Agents must not call each other directly outside this boundary. The orchestrator dispatches through the A2A layer even for in-process transport.

## 7. The Routing Cache Never Skips Visibility

The routing cache stores query-to-agent routing decisions. A routing-cache hit may skip reclassification, but it must not skip entity visibility enforcement or action-cache replay validation. The live execution path must still be used when a routing hit does not have a safe action-cache entry.

## 8. Plugins and Custom Agents Are Trusted but Scoped

Plugins run in-process as trusted code. The supported plugin API is scoped to agent discovery, orchestrator-bound dispatch, settings, MCP registry access, events, and route helpers. This is not a security sandbox: plugins can still reach Python internals unless additional enforcement is added. Runtime must reject conflicting agent IDs, and supported plugin integrations must re-enter work through the orchestrator/A2A path rather than calling peer agents or Home Assistant directly. Custom agents loaded from the database register as `custom-{name}` A2A agents and remain limited to their LLM prompt plus assigned MCP tools.

## 9. Async All the Way Down

No blocking I/O is permitted on the asyncio event loop. All Home Assistant REST and WebSocket calls, cache operations, database access, entity-index operations, and agent dispatch are async. Blocking or CPU-intensive operations must be pushed off the event loop.

## 10. Current State Only in Project Docs

Documentation, code comments, and analysis must describe the current runtime state of the project. Roadmap items must be clearly labeled as such and kept in `docs/roadmap.md`. Never describe planned or aspirational behavior as if it were already implemented.

## 11. No Hardcoded Keyword Routing for Primary Intent Decisions

Primary intent detection, target-agent routing, and top-level intent classification must not rely on hardcoded keyword lists, substring dispatch, regex-only routing, or language-specific phrase tables. This ban is scoped to top-level classification and routing decisions only; downstream executor safeguards and domain validation logic are not prohibited by this directive.

## 12. LLM Owns Live Intent Classification Outside Valid Routing-Cache Hits

When no valid routing-cache decision exists for a turn, the orchestrator must perform live LLM-based intent classification. Valid routing-cache hits may be reused and may skip reclassification, but this exception does not permit bypassing visibility enforcement, action-cache replay validation, or other runtime safety checks.

## 13. Few-Shot Examples in Prompt Assets Must Be English-Only

Few-shot examples used in prompts, classifier templates, and instruction assets must be written in English. This policy does not authorize translating user-provided entity, room, area, device, or location names; user identifiers must remain verbatim where runtime behavior requires preservation.
