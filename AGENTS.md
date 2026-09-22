# AGENTS.md

Operating standard for AI agents working in this repository (`HA-AgentHub` —
multi-agent Home Assistant assistant: a Docker container execution engine plus a
thin HACS-installed Home Assistant bridge).

Naming: product `HA-AgentHub`; repo/image/package slug `ha-agenthub`; HA
integration domain `ha_agenthub`. Legacy `agent-assist`/`agent_assist`
identifiers (DB path, compose volume, cookie names, cache export tags) are
intentional backward compatibility — do not "fix" them ad hoc.

## Initial setup (first contact)

Setup ran on 2026-09-22; this section stays as the contract for any future
repository adopting this file.

Run exactly once per repository: on the first session that finds `brain/BRAIN.md`
missing. If `brain/BRAIN.md` exists and this file is fully adapted, setup already
happened — read the brain first and work normally. Never re-run setup over an existing
brain; if the brain exists but this file still contains placeholders, finish the
interrupted adaptation instead of recreating the brain.

Setup exists so that every later session starts with accurate context instead of
guesses: it researches the repo, asks the user for mission and goals, creates the
brain, and adapts this file.

### Procedure

1. **Confirm first contact** as described above.
2. **Research the repo — through a subagent.** Delegate one exploration prompt per the
   core rule below. Require a structured report, not a file dump:
   - languages, frameworks, key dependencies;
   - build, run, test and verification commands that actually work;
   - entry points and directory layout: what lives in which root;
   - existing docs and what each one owns; obvious docs that are missing;
   - git remote (needed for the jcodemunch repo id), branching and commit conventions
     visible in recent history;
   - the version carrier (`VERSION.md`, `package.json`, tag-only, …);
   - intake channels for bug reports or feedback, if any;
   - naming pitfalls worth protecting.
3. **Ask the user for mission and goals.** These are human decisions. Present the
   research summary and ask why the project exists, what winning looks like, and the
   current top goals. Propose drafts from repo evidence where it is solid; mark
   everything else as an open question. Do not invent a mission.
4. **Create the brain** per the file contracts in the Docs Discipline table:
   `BRAIN.md` (front door: mission summary, reading order, links), `Mission.md`,
   `Goals.md`, `Decisions.md`, `How We Work.md`, and `roadmap.md` as an empty board
   (columns: `Backlog`, `In Progress`, `Testing`, `Done`, `Idea Bank`; cards as
   `- title · priority: … · area: …` lines). Seed `Decisions.md` with the setup
   decisions themselves: tooling, doc map, conventions, verification commands.
5. **Adapt this file.** Replace every placeholder and setup comment:
   - Header: real project name, one-line purpose, naming pitfalls.
   - Project layout: the real roots and their owning docs.
   - Conventions: the repo's reading and authoring rules.
   - Docs Discipline: the owning-docs table mapped to real files; delete rows for docs
     that neither exist nor are planned.
   - Release & Git: version carrier, verification checklist, commit types and scopes
     taken from actual history.
   - Intake: keep, rename, or delete the external-reports section to match reality.
   - Code exploration: adjust the ignored-directories note to this repo.
   - Knowledge library: fill in the project's library topic; delete the section if no
     Athenaeum server is configured for this repo.
   The generic operating rules — delegation, jcodemunch and Athenaeum usage, language,
   vision, docs closeout, the no-commit rule — are never weakened or removed. Setup
   adapts facts, not the operating model.
6. **Verify and hand over.** Re-read this file end to end: no placeholder left, no
   contradictions between sections, brain files render and their links resolve.
   Report to the user what was created, what was assumed, and which open questions
   remain.

### Setup rules

- Setup obeys the core rule like any other task: research and verification go to
  subagents; the main session decides, writes the brain, and edits this file.
- A half-configured brain is worse than none: if setup is interrupted, either finish
  it in the same session or leave a note in `brain/BRAIN.md` stating exactly what is
  missing.
- Never present drafted assumptions as user-confirmed facts.

## Project brain (session context)

`brain/BRAIN.md` is the front door to the project's shared context: mission, current goals,
decisions with rationale, and the live pipeline. Read it first for real work in this repo
and follow links only as far as the task needs. If it does not exist, run the initial
setup above before doing anything else. `brain/roadmap.md` is consumed by a Kanban
view — keep its board format (headings as columns, `- title · priority: … · area: …` lines
as cards).

Keep the brain current as you work — it is only useful while it is accurate:

- Starting or finishing roadmap work → move the card in `brain/roadmap.md`. Cards stay in
  `Testing` until the user has verified the result; only the user moves them to `Done`.
- Making or changing a decision → record it with its rationale in `brain/Decisions.md`.
- Shifting priorities → update `brain/Goals.md`; shipping a release also updates the
  version carrier (see Release & Git).
- Learning something durable → file it in the right brain note and link it there;
  lessons that outlive this repo belong in the knowledge library (see below).

Keep task tracking light:

- Backlog, In Progress and Testing cards state the intended outcome, next step or blocker,
  and how to verify it. Short indented text under the card is enough.
- Testing cards give the user a concrete review target (a link or command to run) and
  say what still needs their verification. Do not present planned checks as passed.
- Detail notes are optional; link one when scope, dependencies or evidence no longer fit
  clearly on the card. Idea Bank cards need no additional template.
- Goals explain the current outcome and the next few priorities; the roadmap owns task
  status and next steps. Decisions record choices and rationale, not copies of rules.

The brain indexes and summarizes; the operating rules themselves stay in this file — do not
move them out, because only `AGENTS.md` is injected into every agent session.

## Core rule: delegate to subagents

**Do not do exploration, research, or bulk implementation work in the main session.**
The main session's context is a scarce resource — spend it on planning, decisions,
and reviewing results. Push everything else into subagents.

### Delegate by default

Spawn a subagent when a task is any of the following:

- **Search / exploration** — "where is X defined", "how does the scene loader work",
  "which models exist", reading several files to answer one question.
- **Research** — looking up references, libraries, APIs, or external documentation.
- **Bulk or repetitive edits** — the same change across many files, mechanical
  refactors, renames, formatting passes.
- **Self-contained implementation** — a module, component, or effect that can be
  described in a standalone prompt.
- **Verification** — running builds, checks, or reviewing a diff against a spec.

### Keep it in the main session

Handle these directly, because they need the accumulated conversation context:

- Clarifying the user's intent and negotiating scope.
- Architecture and design decisions, tradeoffs, API contracts.
- Reviewing and integrating subagent output.
- Final answers, summaries, and anything the user is waiting on.

### How to delegate well

1. **Write a complete, standalone prompt.** A subagent does not see this conversation.
   Include: the goal, the relevant file paths, the constraints, the expected output
   format, and how to verify success.
2. **Give it one job.** Narrow scope beats a vague "improve the codebase".
3. **Run independent subagents in parallel** in a single message, so they work while you
   keep going.
4. **Ask for compressed results.** Request a summary, a diff, or a short structured
   report — never a dump of everything it read.
5. **Demand evidence.** Require exact file paths and line references so you can spot-check
   without re-reading the whole tree.
6. **Do not duplicate a running subagent's work.** While it runs, do something
   independent; do not redo its search yourself.

### Anti-patterns

- Reading dozens of files into the main session to answer one narrow question.
- Doing a mechanical multi-file edit by hand in the main session.
- Handing a subagent a prompt that depends on unstated context, then guessing at its result.
- Letting a subagent report "done" without verification steps you can check.

## Code exploration (jcodemunch)

Use the jcodemunch MCP server for code navigation instead of `grep`/`glob`/`read` sweeps.

- **Repo id:** derived from the git remote, e.g. `owner/repo-name`
  (without a remote: `local/<folder-name>`). Confirm per machine with
  `resolve_repo { path: "<absolute repo path>" }`; do not assume the folder name.
- **Per-machine setup:** run `index_folder` once with this machine's absolute repo path;
  the index lives server-side, so every environment indexes its own checkout.
- **Always pass absolute paths.** Relative paths resolve against the jcodemunch server's
  working directory, not this workspace, and silently resolve to the wrong repo.
- **Entry points:** `menu(query)` to discover actions, `order(action, args)` to run one,
  `route(task)` to pick the action for a goal, `jcodemunch_guide` for the full catalogue.
- **Read-only navigation:** `search_symbols`, `get_file_outline`, `get_symbol_source`,
  `get_context_bundle`, `find_references`, `search_text`, `get_repo_outline`.
- **Use `Read` only immediately before editing a file** — the harness requires a read before
  `Write`/`Edit`. Explore with jcodemunch, then read exactly the file being changed.
- **Gitignored paths are not indexed** — `.venv*/`, `*_cache/`, `container/data/`,
  `secrets/`, `docs/SubAgent/`, `node_modules/`; vendored dashboard assets under
  `container/app/dashboard/static/vendor/` may be partial. Read them directly if
  genuinely needed; never edit generated/runtime artifacts.
- **After edits:** `register_edit` with the changed paths, batched for bulk changes. Re-run
  `index_folder` (state change) after structural changes: new modules, renames, moves.
- **Interpret results literally:** `no_implementation_found` is evidence of absence — report
  the gap, do not re-search with different wording. `degraded` means absence is not proven.

Subagent prompts must state the repo id, the absolute source root, and this tooling rule —
children do not inherit it.

## Knowledge library (Athenaeum MCP)

This project runs an Athenaeum instance as MCP server — a shared, durable knowledge
library across sessions, agents, and repos. A librarian agent curates it; agents ask
for knowledge by intent instead of browsing files.

- **Recall:** `request_knowledge(query)` at session start and before non-trivial
  decisions. There is no browse tool — orientation questions ("what exists on X?")
  go through the same call; `context` narrows the answer.
- **Store:** `store_knowledge(content, topic_hint, kind_hint, relates_to)` persists NEW
  durable knowledge — lessons, patterns, decisions worth keeping. The librarian decides
  placement, frontmatter, and linking. `topic_hint` names the target topic area and is
  required by default; use `ha-agenthub` for this project.
- **Correct:** `update_knowledge(instruction)` changes or corrects EXISTING knowledge;
  the librarian locates the target concepts. `relates_to` back-links related concepts.
- **Health:** `library_status` is a deterministic health report (no LLM).
  `library_maintain` repairs graph health and `library_curate` fixes taxonomy and
  consolidates duplicates; both are no-ops when the library is healthy.
- **Trust and staleness:** concepts carry trust tiers (unverified / machine-confirmed /
  human-reviewed) and staleness flags. Weigh them before relying on a concept; when the
  library and the repo disagree, the repo is the source of truth.
- **Brain vs. library:** `brain/` owns this project's live context (mission, goals,
  decisions, pipeline); the library owns durable knowledge that outlives one repo or
  session. File a lesson once — in the library — and link it from the brain note when
  it matters here.
- **Delegation boundary:** subagents report lessons and findings back; the main session
  decides what is durable enough to store or correct — children do not write to the
  library.
- **Unavailable:** if the server is unreachable, file learnings in the brain as usual
  and continue; sync them into the library when it is back.

## Language and formatting

### No emojis

**Do not use emojis anywhere.** Not in code, comments, docstrings, log or error messages,
commit messages, documentation, reports, or chat replies. No decorative pictographs, no
status glyphs like checkmarks or warning signs, no emoji in place of words.

- Write "done", "failed", "warning", "note" instead of a symbol.
- Use plain ASCII markers where a marker is genuinely useful: `OK`, `FAIL`, `TODO`,
  `FIXME`, `NOTE`.
- If a human's message contains emojis, do not mirror them in your reply.

### Code and comments in English

**All code is written in English, always** — regardless of the language used in the
conversation, the ticket, or the request.

This covers:

- identifiers: variable, function, class, file and directory names,
- comments and docstrings,
- log, warning and error messages,
- commit messages, branch names, and PR descriptions,
- test names and fixture data labels,
- in-repo documentation and code-adjacent notes.

Rules of thumb:

- Conversational replies may follow the human's language, but **code and comments stay
  English** even when the request was in another language.
- Never mix languages inside a single file: no German identifiers or comment lines in a
  file that is otherwise English.
- Do not transliterate existing German identifiers ad hoc; if a rename is warranted,
  propose it as one consistent, verified change.
- When delegating, state this requirement explicitly in the subagent prompt — children do
  not inherit it automatically.

## Vision and multimodal tasks

Image input is **not guaranteed** by the active model. When a task requires looking at an
image — a screenshot, a reference photo, a rendered frame, a sprite sheet, a design mockup —
and the active model does not accept image input, **do not skip the task and do not guess
from filenames**.

Instead:

1. **Detect the limitation early.** If an image read is rejected because the model does not
   support image input, treat that as a signal to delegate, not as a failure to report.
2. **Hand over everything as text.** The child cannot see this conversation. Include:
   - absolute or workspace-relative **paths** to every image it must inspect,
   - the exact question to answer about each image,
   - the desired output shape (short structured report, checklist, JSON, diff proposal).
3. **Ask for text back.** The child returns a written description, findings, or a proposed
   change — never the image itself. Consume that text in the main session.
4. **Route bulk visual work the same way.** Comparing many rendered frames, auditing a set of
   assets for style-guide compliance, or extracting palettes from reference art are all
   fan-out jobs: delegate them, one image or one asset per child.

Rules of thumb:

- Vision-capable subagent for **seeing**; main session for **deciding**.
- State in the child's prompt that it must report uncertainty explicitly — never let it
  invent detail it cannot actually see in the image.
- If no vision-capable model is available, say so plainly and ask the human how to proceed
  rather than producing speculative output.

## Project layout

- `container/` — FastAPI execution engine (`app/`, `tests/`, `plugins/`,
  Dockerfile, compose files); owning docs: `docs/architecture.md`,
  `docs/project/project-definition.md`
- `custom_components/ha_agenthub/` — HA bridge integration (mocked tests in
  `custom_components/tests/`); owning docs: `docs/deployment.md`, `README.md`
- `docs/` — user and developer documentation; owning doc: `docs/README.md`
- `scripts/` — CI/build tooling (`ci.py`, `local-ci.ps1`, `build-and-push.ps1`)
- `.agents/skills/` — workflow skills (new-agent, plugin-dev, mcp-server-dev,
  agent-routing-debug, agenthub-logs, agenthub-csrf, ha-debug)
- `secrets/` — local-only credentials (`secrets/.env.local` provides
  `AA_LIVE_*` / `AA_LOCAL_*` / `AA_BASE_URL` for the admin-API skills);
  gitignored, never commit
- `brain/` — session-spanning project context
- `.github/` — CI workflows, dependabot, CODEOWNERS

Module layout and contracts live in the repo's owning docs (see the Docs
Discipline table); project mission and naming context live in
`brain/Mission.md`. `brain/` holds session-spanning context.

## Conventions

- `docs/project/prime-directives.md` is binding — verify every change against it
  before implementing (execution-engine split, entity visibility on every path,
  action-cache visibility recheck, A2A boundary, async-only, English-only
  few-shot prompt examples, no hardcoded keyword routing).
- Read `docs/style-guide.md` before dashboard work; keep CSS variable names
  stable, update hard-coded hex/RGBA literals in lockstep, and increment
  `_STATIC_BUILD`.
- Entity resolution routes through the shared deterministic-first resolver
  (`container/app/entity/deterministic_resolver.py`) — no raw index shortcuts.
- Agents return executed results, not tool-call plans. New domain agents follow
  the `new-agent` skill and must be imported in
  `container/app/agents/__init__.py` (the `@agent` decorator runs at import
  time).
- Prompt assets live in `container/app/prompts/` (the `new-agent` skill shows a
  stale path — trust the repo).
- `container/data/` is runtime-only; `container/plugins/` is the user drop-in
  dir (files starting with `_` are ignored). Never edit generated or runtime
  artifacts.
- The dashboard frontend is server-rendered Jinja2 + HTMX with vendored JS —
  there is no npm/node toolchain.
- Keep the version in sync across `VERSION.md`, `container/app/__init__.py`, and
  `custom_components/ha_agenthub/manifest.json`.
- Python 3.12, ruff line-length 120; async all the way down — no blocking I/O on
  the event loop.

## Docs Discipline

**Closeout rule:** Every meaningful change requires a docs pass before the task is done.
Update the closest owning doc when a change affects contracts, workflows, structure,
ownership, or operating rules — and remove stale or contradictory text immediately. Small
edits that change no behavior or contract may leave docs unchanged, but the pass still
happens.

**Owning docs** — each rule lives in exactly one of them:

| Doc | Owns |
|---|---|
| `AGENTS.md` | Agent operating rules: setup, delegation, code exploration and knowledge library tooling, language, vision, docs, report triage, release process |
| `README.md` | Product overview, agent inventory, quick start, repo structure |
| `VERSION.md` | Version carrier and changelog |
| `TODO.md` | Long-term idea backlog and unscheduled candidates |
| `SECURITY.md` | Vulnerability reporting |
| `docs/README.md` | Doc map, doc naming, reading order |
| `docs/project/project-definition.md` | Authoritative description of the system as it exists today |
| `docs/project/prime-directives.md` | Binding architecture constraints |
| `docs/project/lessons.md` | Athenaeum pointer + offline fallback facts |
| `docs/architecture.md` | Components, A2A protocol, request/data flow |
| `docs/api-reference.md` | HTTP/WebSocket API surface |
| `docs/configuration.md` | Settings and configuration reference |
| `docs/deployment.md` | Install and deployment (compose, HACS) |
| `docs/user-guide.md` | End-user operation |
| `docs/plugin-development.md` | Plugin authoring contract |
| `docs/backup-restore.md` | Backup/restore procedures |
| `docs/troubleshooting.md` | Operational troubleshooting |
| `docs/style-guide.md` | Dashboard design tokens and visual conventions |
| `docs/CHANGELOG_ARCHIVE.md` | Pre-1.14 changelog history |
| `.agents/skills/*/SKILL.md` | Workflow-specific skills (new-agent, plugin-dev, debugging) |
| `brain/Mission.md` | Why the project exists, what winning looks like |
| `brain/Goals.md` | The few outcomes that matter this stretch |
| `brain/How We Work.md` | Build rhythm; points at the doc map for standards |
| `brain/Decisions.md` | Decision rationale, so decisions stay made |
| `brain/roadmap.md` | The live pipeline, in board format |

**Style rules for all project docs:**

- Keep docs concise, current, and operational — document stable contracts, not diary entries.
- Prefer direct bullets with explicit names over prose.
- Do not duplicate rules across files; each rule lives in exactly one owning doc.
- Delete stale notes instead of explaining history.
- Trim obvious statements, repeated rules, misplaced detail, and warnings for risks that no
  longer exist.

## External reports

External reports arrive as GitHub issues on `mainzerp/ha-agenthub` (no issue
templates exist yet). Security vulnerabilities arrive privately through GitHub
Security Advisories per `SECURITY.md` — never file those as public issues.
Operational evidence additionally comes from the live Admin API (remote-log
ingest, traces) — see the `agenthub-logs`, `ha-debug`, and
`agent-routing-debug` skills.

- The issue owns the findings detail; label `bug` or `enhancement`.
- The roadmap stays a pipeline: a card appears only when an issue is scheduled,
  and the card and the fixing commit reference the issue number.
- Close the issue with a reference to the fixing commit once resolved.

## Release & Git

Do not commit unless the user asks. A roadmap release card is tracking, not authorization
to commit or publish.

**Semantic Versioning:** `MAJOR.MINOR.PATCH`.

- MAJOR = breaking changes requiring user action: changed conversation WS/REST
  contracts, renamed or removed settings keys or API fields, A2A envelope or
  plugin-API breakage, DB migrations needing manual intervention.
- MINOR = backward-compatible additions: a new feature, agent, module, or option.
- PATCH = bug fixes and small improvements: corrections, tweaks, performance, docs.

`VERSION.md` is the single source of truth for the version. It is mirrored in
`container/app/__init__.py` (`__version__`) and
`custom_components/ha_agenthub/manifest.json` — bump all three together. Git
tags `vX.Y.Z` trigger the release pipeline (docker build, Trivy scan, GitHub
Release). No pre-release tag convention is in use.

Release checklist (all required):

- [ ] `VERSION.md` created or bumped, with a history entry listing key
      features/fixes and commit references; `container/app/__init__.py` and
      `custom_components/ha_agenthub/manifest.json` bumped to match. New
      features are tracked there as they are implemented.
- [ ] Git tag matches the version (`vX.Y.Z`) and points at the release commit.
- [ ] Release has an explicit title and notes listing every new feature, changed
      behavior, and removal. Auto-generated notes are a starting point, not a substitute.
- [ ] Verification passes: `ruff check` + `ruff format --check` on `container/`
      and `custom_components/`; `cd container && python -m pytest tests/ -q`
      (background on Windows; verdict from the printed summary);
      `python -m pytest custom_components/tests/ -n auto`;
      `python scripts/ci.py` for the full gate. `mypy` is report-only.
- [ ] Docs closeout done for the change (see Docs Discipline above).

**Conventional Commits:** `<type>(<scope>): <short summary>`.

- Types: `feat` (MINOR bump), `fix` (PATCH bump), `chore` (maintenance/deps), `docs`,
  `refactor`, `test`, `release` (version bump).
- Scopes seen in history (prefer these over inventing new ones): `core`,
  `orchestrator`, `agents`, `entity`, `entity-resolution`, `cache`, `routing`,
  `memory`, `embedding`, `llm`, `conversation`, `integration`, `config-flow`,
  `dashboard`, `trace`, `logs`, `skills`, `deps`, `deps-dev`, `docs`.
- Release commits use the canonical form `release: bump version to X.Y.Z`.
- Summary under 72 characters, imperative mood ("add X"), reference issues where
  applicable (`fix(core): correct edge case (#42)`).
