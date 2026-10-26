# Release

Versioning and release checklist for HA-AgentHub.

## Versioning

Semantic Versioning `MAJOR.MINOR.PATCH`.

- MAJOR: breaking changes that need user action: changed conversation WS/REST contracts, renamed or removed settings
  keys or API fields, A2A envelope or plugin-API breakage, DB migrations that need manual intervention.
- MINOR: backward-compatible additions: a new feature, agent, module or option.
- PATCH: fixes, tweaks, performance, docs.

`VERSION.md` is the single source of truth. It is mirrored in `container/app/__init__.py` (`__version__`) and
`custom_components/ha_agenthub/manifest.json`; bump all three together, then re-run `python scripts/sync_orb_card.py`
(the card embeds the build marker). No pre-release tag convention is in use.

## Checklist

All required:

- `VERSION.md` bumped, with a history entry listing key features and fixes with commit references (new features are
  tracked there as they land); both mirrors match.
- Release commit `release: bump version to X.Y.Z`; git tag `vX.Y.Z` points at it. The tag triggers the release
  pipeline (docker build, Trivy scan, GitHub Release).
- The GitHub Release has an explicit title and notes listing every new feature, changed behavior and removal
  (generated notes are a start, not a substitute).
- Verification passes: the full gate `python scripts/ci.py` (commands in `AGENTS.md`, Tool paths).
- Docs closeout done.
