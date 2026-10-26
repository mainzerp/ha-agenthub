# How We Work

Build rhythm and day-to-day mechanics. Operating rules, commands and tool paths
live in `AGENTS.md`; doc ownership lives in its Docs Discipline table and in
`docs/README.md`; releases follow `docs/release.md`.

## Flow

- Trunk-based on `main`; short-lived `feat/...` branches; `dependabot/*` for
  dependency PRs (weekly, pip+docker in `/container`, github-actions in `/`).
- Work lands through PRs; a release is its own `release/X.Y.Z` PR.
- Before calling work done: ruff and the touched test suites pass (commands in
  `AGENTS.md`, Tool paths); before a release the full `scripts/ci.py` gate.

## Environment notes

- Live instance: `http://192.168.120.200:6081`, debugged through the admin-API
  skills only.
- Deps in `container/requirements-dev.txt`; venv at `.venv/`.
