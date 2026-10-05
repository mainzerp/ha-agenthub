# Decisions

Decision rationale, so decisions stay made. Newest first. Binding architecture
rules live in `docs/project/prime-directives.md` — this file records choices,
not copies of rules.

## 2026-10-04 — Few-shot examples in the configured language (Directive 13 amended)

Directive 13 now allows per-language few-shot assets with English as the
default and fallback. It records a choice already made in `f201778`
(2026-09-05), which split the orchestrator agent catalog into
`orchestrator_examples_<lang>.txt` (de, en, es, fr, it). Rationale:

- **English-only examples did not always route correctly.** Smaller local
  models misrouted non-English utterances; the 'Ambiente Wohnen'
  misrouting class is the evidence. Examples in the configured language
  passed the 17 of 17 routing battery against qwen-3.8-27b on the live
  deployment.
- **The guard rails stay.** Language variants live only in per-language
  asset files, and the English file is the default and fallback. No
  non-English phrases are hardcoded in Python or in shared templates, so
  Directive 11 (no language-specific phrase tables for routing) is still
  met.
- **Follow-up:** `classification_engine.py`
  `cancel_interaction_description_line()` hardcodes German examples
  (abbrechen/egal/schon gut) in Python. Move them into the per-language
  catalogs.

## 2026-09-29 — Multi-action execution contract

Domain-agent LLMs may emit one fenced JSON block per requested action
("A und B ausschalten"). `parse_actions()` collects all valid blocks
(dedupe, cap 8); `ActionableAgent` executes them sequentially in
utterance order, each with its own `ha_action` span. Rationale:

- **Sequential, not parallel:** execution order must match the user's
  utterance and keep trace spans ordered; per-action exceptions degrade
  to a per-action error result without aborting the rest.
- **First-action headline:** `TaskResult.action_executed` still reports
  the first action (public `ActionResult` contract unchanged);
  `TaskResult.actions_executed` + `ActionExecuted.entity_ids` carry the
  full set for consumers that care.
- **Multi-action turns are never action-cached** — a single
  `CachedAction` cannot replay N service calls. They store a routing
  entry only (all entity_ids forwarded, visibility rechecked per entity
  on hits — Directive 2 intact). Partial failures are not turn errors;
  only an all-failed turn attaches an `AgentError`.
- Evidence: trace `1f01c7feff2e411e` (2026-09-28) — the model emitted
  two correct blocks and only the first executed; Athenaeum lesson
  `live-debugging/multi-action-dropped-by-single-action-contract-trace-2026-09-28`.

## 2026-09-22 — Agent operating setup (initial brain creation)

Decisions taken while adapting `AGENTS.md` and creating `brain/`:

- **Version carrier:** `VERSION.md` is the single source of truth; the version
  is mirrored in `container/app/__init__.py` (`__version__`) and
  `custom_components/ha_agenthub/manifest.json`. All three are bumped together.
  Rationale: three consumers (changelog reader, container runtime, HACS) each
  need the version in their own format; history shows them kept in lockstep.
- **Release commit form:** `release: bump version to X.Y.Z` (canonical; owner
  confirmed). History contained three spellings; pick one and stay consistent.
  Tag `vX.Y.Z` triggers the release pipeline.
- **Verification gate:** `ruff check` + `ruff format --check` on `container/`
  and `custom_components/` must pass; `pytest` suites in `container/` and
  `custom_components/tests/` must pass (coverage gate 80). `mypy` is report-only,
  non-blocking — an observed standing convention, not a target to silently
  tighten. `python scripts/ci.py` runs the full local gate.
- **Doc map:** `docs/README.md` created as the doc index; ownership table lives
  in `AGENTS.md` (Docs Discipline). `docs/project/project-definition.md` remains
  the authoritative system description; `TODO.md` keeps the long-term idea
  backlog while `brain/roadmap.md` owns the live pipeline.
- **Tooling:** jcodemunch repo id `mainzerp/ha-agenthub` (indexed on this
  machine, source root `F:\Github\ha-agenthub`). Athenaeum topic for this
  project: `ha-agenthub`.
- **Conventions carried into AGENTS.md:** prime-directives are binding;
  `docs/style-guide.md` governs dashboard work; `container/data/` and
  `container/plugins/` are runtime/user dirs; never delete Docker volumes;
  `secrets/.env.local` holds live/local credentials for the admin-API skills.

## Open questions (asked at setup, not yet decided)

- Whether the mypy baseline is intentionally permanent or being burned down.

Resolved 2026-09-22: `SECURITY.md` supported-version table updated to 2.4.x;
`secrets/.env.local` exists locally and stays local (gitignored); all six
drifted skill docs plus `container/plugins/README.md` corrected after a full
audit (agent registration is declarative via `@agent`/`install_all_agents`,
prompts live in `container/app/prompts/`, `ctx.agent_registry` not
`agent_catalog`, `PUT /api/admin/settings/{key}`, `POST /api/admin/cache/flush`,
`/api/admin/mcp-servers`, logs `limit` cap 1000, match-preview returns
`deterministic`/`hybrid`/`visibility`/`diagnostics`).

## 2026-09-22 — noise route for false-trigger silence

- **`noise` pipeline directive added** (alongside `cancel-interaction`, never a
  real agent): the orchestrator may classify contextless fragments / background
  chatter (TV bleed-through) as `noise`. A sole-noise turn early-exits before
  dispatch with empty speech — no action, no TTS, no conversation turn — and
  restores any pending clarifying question the prelude consumed. Rationale:
  accidental wake-word activations from TV audio produced disruptive spoken
  replies; routing them to a silent route beats TTS-suppression heuristics
  because the classify LLM sees conversation context (a bare "Küche." stays a
  valid answer when a question is pending). Evaluated and rejected
  cactus-needle as an external noise gate first (67% false negatives on real
  German commands, confident false positives on noise; see library entry
  `ha-agenthub/cactus-needle-3.0.4-false-trigger-gate-lessons`).

## 2026-10-05 — rewrite failures are failures; fallback is mediated text

- **A failed cache-hit rewrite is recorded as failed** (rewrite span status
  error, `success: false`, analytics failure) instead of returning the input
  text as a fake success. Rationale: a provider 404 went unnoticed for over a
  week because traces and analytics showed 100% rewrite success.
- **Fallback speech is the stored mediated `response_text`**, not the raw
  agent text. Rationale: the raw text is the English executor template; the
  mediated text is what the user already heard, in their language and tone.
- **rewrite-agent defaults to `reasoning_effort: "none"`** via agent config
  (seed + migration 44, only where unset), not hardcoded. Rationale: the 2 s
  timeout leaves no room for thinking; the admin keeps control per model,
  since some reasoning models (gpt-oss, gpt-5) reject "none".
