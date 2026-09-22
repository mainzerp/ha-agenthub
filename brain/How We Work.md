# How We Work

Build rhythm and day-to-day mechanics. Operating rules live in `AGENTS.md`;
doc ownership lives in `docs/README.md` and the Docs Discipline table.

## Flow

- Trunk-based on `main`; short-lived `feat/...` branches; `dependabot/*` for
  dependency PRs (weekly, pip+docker in `/container`, github-actions in `/`).
- Conventional Commits: `<type>(<scope>): <summary>` — see `AGENTS.md`
  Release & Git for the type/scope list. Release commits use the canonical form
  `release: bump version to X.Y.Z`.
- Do not commit unless the user asks.

## Verification (before calling work done)

- `cd container && ruff check . && ruff format --check .` (same for
  `custom_components/`) — must pass.
- `cd container && python -m pytest tests/ -q` — ~330 s sequential; on Windows
  run it as a background task and take the verdict from the printed summary
  (the process can hang at interpreter shutdown after printing).
- `python -m pytest custom_components/tests/ -n auto` — HA module is mocked,
  no real install needed.
- `mypy container/app` — report-only; check the baseline against pristine HEAD
  before claiming a regression.
- Full gate: `python scripts/ci.py` (`--skip-docker` without Docker).

## Environment notes

- Local run: `docker compose -f container/docker-compose_local.yml up -d --build`.
  Never delete Docker volumes (`down -v`, `volume rm`) unless explicitly asked.
- Live instance: `http://192.168.120.200:6081`; admin-API debugging via the
  `agenthub-csrf`, `agenthub-logs`, `ha-debug`, `agent-routing-debug` skills.
  Credentials live in `secrets/.env.local` — gitignored, local only, never
  commit.
- Python 3.12; deps in `container/requirements-dev.txt`; venv at `.venv/`.
