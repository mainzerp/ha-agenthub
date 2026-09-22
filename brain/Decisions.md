# Decisions

Decision rationale, so decisions stay made. Newest first. Binding architecture
rules live in `docs/project/prime-directives.md` — this file records choices,
not copies of rules.

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
