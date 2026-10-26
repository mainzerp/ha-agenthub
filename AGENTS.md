# AGENTS.md

Operating rules for AI agents in `HA-AgentHub`, a multi-agent Home Assistant assistant: a Docker container execution
engine plus a thin HACS-installed Home Assistant bridge. Naming: product `HA-AgentHub`; repo, image and package slug
`ha-agenthub`; HA integration domain `ha_agenthub`. Legacy `agent-assist`/`agent_assist` identifiers (DB path, compose
volume, cookie names, cache export tags) are intentional backward compatibility; do not rename them ad hoc.

## Project brain

`brain/BRAIN.md` is the front door to mission, goals, decisions and the live pipeline. Read it first for real work
and follow links only as far as the task needs. If it is ever missing, recreate it from the owning-docs table and ask
the user for mission and goals; never present drafted assumptions as confirmed.

Keep it current while you work:

- Roadmap work starts or ends: move the card in `brain/roadmap.md` and keep its board format (headings as columns,
  `- title · priority: … · area: …` lines as cards; a Kanban view reads it). Cards stay in `Testing` until the user
  has verified them; only the user moves them to `Done`.
- Cards state the outcome, next step or blocker, and how to verify. Testing cards name a concrete review target
  (link or command) and what still needs the user's check; never present planned checks as passed. Link a detail
  note only when scope or evidence no longer fits on the card.
- A decision is made or changed: record it with rationale in `brain/Decisions.md`, newest first. Decisions record
  choices, not copies of rules.
- `Decisions.md` grows hard to read: move entries before a cut-off date verbatim to `brain/Decisions-archive.md`
  and leave one status line per archived entry in an "Archive" section (still valid, superseded by ..., done), each
  pointing at the doc that owns the rule now.
- Priorities shift: update `brain/Goals.md`.
- Operating rules stay in this file; it is the only one injected into every session. A rule that applies to every
  agent belongs here or in an owning doc, not in one machine's agent memory.

## Delegation

The main session plans, decides, talks to the user and reviews results. Delegate to subagents when a task needs more
than a few file reads: search and exploration, research, bulk or mechanical edits, self-contained implementation,
builds and verification.

- Write a standalone prompt: goal, absolute paths, constraints, output format, how to verify. Children inherit
  nothing: state the repo id, the jcodemunch rule, English-only code and no emojis, the prime directives
  (`docs/project/prime-directives.md`) for code changes, and never touching the live instance.
- One job per agent. Run independent agents in parallel (one message, several calls) whenever the work splits; do
  not redo a running agent's work.
- Pick the model by job: exploration and search `haiku`; planning and design `opus`; implementation `sonnet` for
  plain code work, `opus` when it needs creative or design judgment.
- Ask for compressed results with file paths and line references, and verification you can check.
- For an independent review, hand the reviewer the artifact and the code, not your plan or conclusion; an agent
  handed a hypothesis tends to confirm it.
- Images: if the active model cannot read one, delegate to a vision-capable child with the image paths and the exact
  question, and require it to state uncertainty instead of inventing detail. Never guess from file names.

## Code exploration (jcodemunch)

Navigate code with the jcodemunch MCP server instead of `grep`/`glob`/`read` sweeps; the server documents its own
tools.

- Repo id `mainzerp/ha-agenthub`; confirm with `resolve_repo` and run `index_folder` once per machine. Always pass
  absolute paths (relative ones resolve against the server's directory).
- `Read` only the file you are about to edit. After edits call `register_edit`; after new modules, renames or moves
  re-run `index_folder`.
- `no_implementation_found` is evidence of absence; `degraded` is not.
- Not indexed and never hand-edited: `.venv*/`, `*_cache/`, `container/data/`, `secrets/`, `docs/SubAgent/`,
  `node_modules/`; vendored assets under `container/app/dashboard/static/vendor/` may be partial.

## Knowledge library (Athenaeum MCP)

- `request_knowledge(query)` at session start and before non-trivial decisions.
- `store_knowledge` for new durable lessons (`topic_hint`: `ha-agenthub` for project lessons, the technology's topic
  such as `home-assistant` or `python` for general ones); `update_knowledge` to correct existing ones. Weigh trust
  tier and staleness; the repo wins on conflict.
- Project context lives in `brain/`, lessons that outlive the repo in the library; file a lesson once and link it.
- Only the main session writes to the library. If it is unreachable, file in the brain and sync later.

## Language

- No emojis anywhere: code, docs, commits, reports, chat. Use `OK`, `FAIL`, `TODO`, `NOTE` where a marker helps. Do
  not mirror emojis from a user message.
- All code, comments, logs, commits, branch names, PR texts, test names and in-repo docs are English, whatever the
  conversation language. Chat may follow the user's language. Never mix languages in one file; rename existing
  non-English identifiers only as one consistent, verified change.

## Conventions

- Read the owning doc (table below) before changing artifacts in its area. `docs/project/prime-directives.md` is
  binding: check every change against it.
- Entity resolution goes through `container/app/entity/deterministic_resolver.py`; no raw index shortcuts.
- Agents return executed results, not tool-call plans. New domain agents follow the `new-agent` skill and are
  imported in `container/app/agents/__init__.py` (the `@agent` decorator runs at import time). Prompt assets live
  in `container/app/prompts/`.
- Dashboard: server-rendered Jinja2 + HTMX with vendored JS, no npm toolchain. Read `docs/style-guide.md` first; keep
  CSS variable names stable, update hard-coded hex/RGBA literals in lockstep and increment `_STATIC_BUILD`.
- Generated copies are rebuilt, never hand-edited; `ci.py` fails on stale ones:
  - `container/app/knowledge/*.md` from the user docs: `python scripts/sync_self_docs.py`.
  - `custom_components/ha_agenthub/frontend/agenthub-activity-orb-card.js` from `container/app/dashboard/static/`:
    `python scripts/sync_orb_card.py`, also after any version or `_STATIC_BUILD` bump.
  - `container/requirements.lock` after changing `container/requirements.txt` or `requirements-torch.txt`:
    `python scripts/ci.py --update-lock`.
- `container/data/` is runtime-only; `container/plugins/` is the user drop-in dir (files starting with `_` are
  ignored).
- Python 3.12, ruff line-length 120, async all the way down: no blocking I/O on the event loop.
- Run ruff check and format on changed Python, plus the relevant test suite; `python scripts/ci.py` before a release.

## Tool paths (this machine)

Run from the repo root `F:\Github\ha-agenthub`; quote paths. The venv `.venv\Scripts\` provides `python` (3.12),
`ruff`, `pytest` and `mypy`.

- Lint: `ruff check container custom_components` and `ruff format --check container custom_components`.
- Container tests: `cd container && python -m pytest tests/ -q` (one test: `tests/<file>.py::<name>`). About 330 s;
  on Windows run it in the background and take the verdict from the printed summary (the process can hang at
  interpreter shutdown).
- Bridge tests: `python -m pytest custom_components/tests/ -n auto` (HA is mocked).
- `mypy container/app` is report-only; compare against pristine HEAD before claiming a regression.
- Full gate: `python scripts/ci.py` (`--skip-docker` without Docker); `scripts/local-ci.ps1` wraps it.
- Local instance: `docker compose -f container/docker-compose_local.yml up -d --build`. Building and testing against
  it is allowed. Never delete Docker volumes (`down -v`, `volume rm`) unless asked.
- `secrets/.env.local` (gitignored, never commit or print): `AA_LOCAL_*` for test traffic, `AA_LIVE_*` for the
  production instance, which the admin-API skills (`agenthub-csrf`, `agenthub-logs`, `ha-debug`,
  `agent-routing-debug`) may read but tests never target.

## Worktrees

- Harness-created agent worktrees (`.claude/worktrees/`) may branch from `main`, not from HEAD. When the base
  matters, create the worktree by hand (`git worktree add -b <branch> <path> <base>`) and pass its path.
- Never junction or symlink `secrets/` or `.venv/` into a worktree; read them by absolute path. Before removing a
  worktree, check it for links and never force-remove one that has them.
- The pre-commit and pre-push hooks (`.pre-commit-config.yaml`: ruff, bandit, pip-audit, pytest) run with
  `language: system` and need the repo venv on PATH. A worktree has no `.venv`: run its checks with
  `F:\Github\ha-agenthub\.venv\Scripts\python.exe` and leave pushing to the user or the main checkout.

## Docs Discipline

Every meaningful change ends with a docs pass: update the owning doc when contracts, workflows, structure or rules
change, and delete stale or contradictory text. A change that alters no behavior or contract may leave the docs as
they are, but the pass still happens. Each rule lives in exactly one owning doc. Docs stay concise and operational:
stable contracts as bullets, explicit names, no history, no repeated rules. Split a doc that grows past one area
into per-area docs and update the table.

Drift check (doc against code):

- When: before a card that changed an area's contracts moves to `Testing` (for each owning doc it touched), and
  whenever the user asks for one (`drift check docs/<doc>.md`).
- Who: one zero-context subagent (`sonnet`) per doc. Its prompt has only the doc path, the code paths that doc owns
  (from its table or file index) and the standard child rules. It gets no task history, plan or rationale, so it
  does not start out believing the doc.
- Output, report only: each doc claim the code contradicts or no longer supports (doc line, code `file:line`,
  evidence), each public contract in the owned code that the doc is missing, and each doc reference to a file,
  symbol or flag that does not exist. No style notes.
- The main session spot-checks every finding against the code, then fixes the doc. If the code looks wrong rather
  than the doc, it asks the user instead of changing either.

| Doc | Owns |
|---|---|
| `AGENTS.md` | Agent operating rules, tooling, language, docs discipline, commit rules |
| `README.md` | Product overview, agent inventory, quick start, repo structure |
| `docs/README.md` | Doc map, reading order |
| `docs/project/project-definition.md` | Authoritative description of the system today; `container/` |
| `docs/project/prime-directives.md` | Binding architecture constraints |
| `docs/project/lessons.md` | Athenaeum pointer and offline fallback facts |
| `docs/architecture.md` | Components, A2A protocol, request and data flow |
| `docs/api-reference.md` | HTTP/WebSocket API surface |
| `docs/configuration.md` | Settings and configuration reference |
| `docs/deployment.md` | Install and deployment (compose, HACS); `custom_components/ha_agenthub/` |
| `docs/user-guide.md` | End-user operation |
| `docs/plugin-development.md` | Plugin authoring contract; `container/plugins/` |
| `docs/backup-restore.md` | Backup and restore procedures |
| `docs/troubleshooting.md` | Operational troubleshooting |
| `docs/style-guide.md` | Dashboard design tokens and visual conventions |
| `docs/release.md` | Versioning, release checklist; `scripts/`, `.github/` workflows |
| `.agents/skills/*/SKILL.md` | Workflow skills (new-agent, plugin-dev, mcp-server-dev, debugging, logs, CSRF) |
| `VERSION.md` | Current version and release history; `docs/CHANGELOG_ARCHIVE.md` holds pre-1.14 history |
| `TODO.md` | Long-term idea backlog and unscheduled candidates |
| `SECURITY.md` | Vulnerability reporting |
| `brain/Mission.md`, `Goals.md`, `How We Work.md`, `Decisions.md`, `Decisions-archive.md`, `roadmap.md` | Why, current outcomes, rhythm, decision rationale (archive once needed), live pipeline |

## External reports

- Reports arrive as GitHub issues on `mainzerp/ha-agenthub` (`bug` or `enhancement`); the issue owns the detail.
  Security vulnerabilities arrive privately through GitHub Security Advisories per `SECURITY.md`; never file those as
  public issues.
- Operational evidence comes from the live Admin API (remote-log ingest, traces) via the `agenthub-logs`, `ha-debug`
  and `agent-routing-debug` skills.
- The roadmap stays a pipeline: a card appears only when an issue is scheduled; card and fixing commit reference the
  issue number. Close the issue with a reference to the fixing commit.

## Git

- Do not commit unless the user asks; a roadmap card is not authorization to commit or publish. Releases follow
  `docs/release.md`.
- Conventional Commits `<type>(<scope>): <summary>`, imperative, under 72 characters, issue refs where useful.
- Types: `feat` (MINOR), `fix` (PATCH), `chore`, `docs`, `refactor`, `test`, `release`.
- Scopes: `core`, `orchestrator`, `agents`, `entity`, `cache`, `routing`, `memory`, `llm`, `voice`, `conversation`,
  `integration`, `dashboard`, `trace`, `logs`, `skills`, `deps`, `deps-dev`, `docs`.
