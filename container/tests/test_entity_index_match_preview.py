"""Tests for /api/admin/entity-index/match-preview.

Exercises the match preview endpoint with mocked ``entity_index`` /
``entity_matcher`` so we validate the response shape and the three
blocks (deterministic resolver, hybrid candidates, visibility summary)
without spinning up the full app lifespan.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api.routes import entity_index_api
from app.entity.matcher import MatchResult
from app.security.auth import require_admin_session


def _make_entry(entity_id: str, friendly_name: str, area: str | None = None):
    return SimpleNamespace(
        entity_id=entity_id,
        friendly_name=friendly_name,
        area=area,
        domain=entity_id.split(".", 1)[0],
    )


@pytest_asyncio.fixture()
async def preview_client():
    """Minimal FastAPI app wired with the entity_index router and mocks."""
    app = FastAPI()
    app.dependency_overrides[require_admin_session] = lambda: {"user": "test"}
    app.include_router(entity_index_api.router)

    entries = [
        _make_entry("light.keller", "Keller", area="keller"),
        _make_entry("light.bedroom", "Bedroom", area="bedroom"),
    ]

    entity_index = MagicMock()
    entity_index.list_entries = MagicMock(return_value=entries)
    entity_index.get_by_id = MagicMock(side_effect=lambda eid: next((e for e in entries if e.entity_id == eid), None))

    entity_matcher = MagicMock()
    entity_matcher.match = AsyncMock(
        return_value=[
            MatchResult(
                entity_id="light.keller",
                friendly_name="Keller",
                score=0.92,
                signal_scores={"alias": 1.0, "embedding": 0.81, "levenshtein": 0.9},
            ),
            MatchResult(
                entity_id="light.bedroom",
                friendly_name="Bedroom",
                score=0.72,
                signal_scores={"embedding": 0.72},
            ),
        ]
    )
    entity_matcher.filter_visible_results = AsyncMock(side_effect=lambda a, r: r)

    app.state.entity_index = entity_index
    app.state.entity_matcher = entity_matcher

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, entity_index, entity_matcher


@pytest.mark.asyncio
async def test_match_preview_returns_all_blocks(preview_client):
    client, _ei, _em = preview_client

    resolver_output = {
        "entity_id": "light.keller",
        "friendly_name": "Keller",
        "speech": None,
        "metadata": {
            "query": "keller",
            "resolution_path": "exact_friendly_name",
            "match_count": 1,
            "top_entity_id": "light.keller",
            "top_score": 0.92,
        },
    }

    with (
        patch(
            "app.agents.action_executor._resolve_light_entity",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "keller", "agent_id": "light-agent"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["query"] == "keller"
    assert data["agent_id"] == "light-agent"

    det = data["deterministic"]
    assert det["entity_id"] == "light.keller"
    assert det["friendly_name"] == "Keller"
    assert det["metadata"]["resolution_path"] == "exact_friendly_name"
    assert det["domain_allowed"] is True
    assert det["error"] is None

    assert data["hybrid_error"] is None
    assert len(data["hybrid"]) == 2
    top = data["hybrid"][0]
    assert top["entity_id"] == "light.keller"
    assert top["domain"] == "light"
    assert top["area"] == "keller"
    assert top["score"] == pytest.approx(0.92, abs=1e-3)
    assert "alias" in top["signal_scores"]

    vis = data["visibility"]
    assert vis["agent_id"] == "light-agent"
    assert vis["rules"] == []
    assert vis["total_entity_count"] == 2
    assert vis["visible_entity_count"] == 2


@pytest.mark.asyncio
async def test_match_preview_rejects_empty_query(preview_client):
    client, *_ = preview_client
    resp = await client.get(
        "/api/admin/entity-index/match-preview",
        params={"q": "   "},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_match_preview_surfaces_domain_gate_reject(preview_client):
    client, *_ = preview_client
    resolver_output = {
        "entity_id": "climate.thermostat",
        "friendly_name": "Thermostat",
        "speech": None,
        "metadata": {
            "query": "thermostat",
            "resolution_path": "exact_friendly_name",
            "match_count": 1,
            "top_entity_id": "climate.thermostat",
        },
    }
    with (
        patch(
            "app.agents.action_executor._resolve_light_entity",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "thermostat"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["deterministic"]["entity_id"] == "climate.thermostat"
    assert data["deterministic"]["domain_allowed"] is False


@pytest.mark.asyncio
async def test_match_preview_503_when_index_missing():
    app = FastAPI()
    app.dependency_overrides[require_admin_session] = lambda: {"user": "test"}
    app.include_router(entity_index_api.router)
    app.state.entity_index = None
    app.state.entity_matcher = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "keller"},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_match_preview_climate_agent_allows_climate_entity(preview_client):
    """`climate-agent` must mark a `climate.*` resolution as domain_allowed."""
    client, _ei, em = preview_client
    em.match.return_value = [
        MatchResult(entity_id="climate.wohnzimmer", friendly_name="Wohnzimmer", score=0.9),
    ]
    resolver_output = {
        "entity_id": "climate.wohnzimmer",
        "friendly_name": "Wohnzimmer",
        "speech": None,
        "metadata": {
            "query": "wohnzimmer",
            "resolution_path": "exact_friendly_name",
            "match_count": 1,
        },
    }
    with (
        patch(
            "app.entity.deterministic_resolver.resolve_entity_deterministic_first",
            new=AsyncMock(return_value=resolver_output),
        ) as deterministic_resolve,
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "wohnzimmer", "agent_id": "climate-agent"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["deterministic"]["entity_id"] == "climate.wohnzimmer"
    assert data["deterministic"]["domain_allowed"] is True
    assert "climate" in data["agent_allowed_domains"]
    # `:non_light_agent` suffix should be appended on the resolution path.
    assert data["deterministic"]["metadata"]["resolution_path"].endswith(":non_light_agent")
    deterministic_resolve.assert_awaited_once()
    call = deterministic_resolve.await_args
    assert call.args == ("wohnzimmer", _ei, em, "climate-agent")
    assert call.kwargs == {"allowed_domains": frozenset({"climate", "sensor", "weather"})}


@pytest.mark.asyncio
async def test_match_preview_domain_hard_filter(preview_client):
    """`?domain=climate` must restrict hybrid candidates to that domain."""
    client, _ei, em = preview_client
    em.match.return_value = [
        MatchResult(entity_id="climate.wohnzimmer", friendly_name="Wohnzimmer", score=0.9),
        MatchResult(entity_id="light.wohnzimmer", friendly_name="Wohnzimmer", score=0.8),
    ]
    resolver_output = {
        "entity_id": "light.wohnzimmer",
        "friendly_name": "Wohnzimmer",
        "speech": None,
        "metadata": {
            "query": "wohnzimmer",
            "resolution_path": "exact_friendly_name",
            "match_count": 2,
        },
    }
    with (
        patch(
            "app.entity.deterministic_resolver.resolve_entity_deterministic_first",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "wohnzimmer", "domain": "climate"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["domain"] == "climate"
    assert data["preferred_domains"] == ["climate"]
    assert data["hybrid"], "expected at least one hybrid entry after filter"
    for entry in data["hybrid"]:
        assert entry["domain"] == "climate"
    # Resolved id is in `light` -> dropped flag set, gate flips to False.
    assert data["deterministic"]["metadata"]["domain_filter_dropped"] is True
    assert data["deterministic"]["domain_allowed"] is False
    # Matcher must have received `preferred_domains=("climate",)`.
    em.match.assert_awaited()
    call = em.match.await_args
    assert call.kwargs.get("preferred_domains") == ("climate",)


@pytest.mark.asyncio
async def test_match_preview_response_exposes_agent_allowed_domains(preview_client):
    """Response surfaces the agent's allowed-domain set."""
    client, *_ = preview_client
    resolver_output = {
        "entity_id": None,
        "friendly_name": "anything",
        "speech": None,
        "metadata": {"query": "anything", "resolution_path": "no_match", "match_count": 0},
    }
    with (
        patch(
            "app.agents.action_executor._resolve_light_entity",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "anything", "agent_id": "climate-agent"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data["agent_allowed_domains"]) == {"climate", "sensor", "weather"}
    assert set(data["preferred_domains"]) == {"climate", "sensor", "weather"}


@pytest.mark.asyncio
async def test_match_preview_backward_compat_no_filters(preview_client):
    """No filters: legacy keys preserved; new keys empty/None."""
    client, *_ = preview_client
    resolver_output = {
        "entity_id": "light.keller",
        "friendly_name": "Keller",
        "speech": None,
        "metadata": {
            "query": "keller",
            "resolution_path": "exact_friendly_name",
            "match_count": 1,
        },
    }
    with (
        patch(
            "app.agents.action_executor._resolve_light_entity",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "keller"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    legacy = {"query", "agent_id", "deterministic", "hybrid", "hybrid_error", "visibility"}
    assert legacy.issubset(data.keys())
    assert data["domain"] is None
    assert data["agent_allowed_domains"] == []
    assert data["preferred_domains"] == []
    # Legacy `_validate_domain` semantics: light.* is allowed.
    assert data["deterministic"]["domain_allowed"] is True
    # Resolution path is not annotated when no agent filter applies.
    assert data["deterministic"]["metadata"]["resolution_path"] == "exact_friendly_name"


@pytest.mark.asyncio
async def test_match_preview_area_only_climate_agent_returns_visible_sensor(preview_client):
    """Area-only query under climate-agent must surface a visible sensor in hybrid.

    Locks the route contract that the matcher fix in
    docs/SubAgent/area_only_climate_agent_plan.md must satisfy: when the
    matcher returns a climate-visible sensor for an area-only query, the
    route propagates it through ``hybrid`` with ``area`` populated and
    advertises the climate-agent's preferred domain set.
    """
    client, ei, em = preview_client
    target = _make_entry(
        "sensor.luftsensor_masterbad_am2301_temperature",
        "Luftsensor Masterbad Temperatur",
        area="masterbad",
    )
    ei.list_entries = MagicMock(return_value=[target])
    ei.get_by_id = MagicMock(side_effect=lambda eid: target if eid == target.entity_id else None)
    em.match.return_value = [
        MatchResult(
            entity_id=target.entity_id,
            friendly_name=target.friendly_name,
            score=0.91,
            signal_scores={"embedding": 0.78, "area": 0.30},
        ),
    ]
    resolver_output = {
        "entity_id": None,
        "friendly_name": "masterbad",
        "speech": None,
        "metadata": {
            "query": "masterbad",
            "resolution_path": "no_match",
            "match_count": 0,
        },
    }
    with (
        patch(
            "app.agents.action_executor._resolve_light_entity",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "masterbad", "agent_id": "climate-agent"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["agent_id"] == "climate-agent"
    assert set(data["preferred_domains"]) >= {"climate", "sensor", "weather"}
    assert any(h["entity_id"] == target.entity_id and h["area"] == "masterbad" for h in data["hybrid"]), data["hybrid"]
    em.match.assert_awaited()
    call = em.match.await_args
    assert call.kwargs.get("agent_id") == "climate-agent"


@pytest.mark.asyncio
async def test_match_preview_diagnostics_no_allowed_entities(preview_client):
    """Empty hybrid + no allowed-domain entities -> reason=no_entities_of_allowed_domains."""
    client, ei, em = preview_client
    em.match.return_value = []
    ei._store = MagicMock()
    ei._store.get = MagicMock(return_value={"metadatas": []})
    resolver_output = {
        "entity_id": None,
        "friendly_name": "foo",
        "speech": None,
        "metadata": {"query": "foo", "resolution_path": "no_match", "match_count": 0},
    }
    with (
        patch(
            "app.entity.deterministic_resolver.resolve_entity_deterministic_first",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "foo", "agent_id": "climate-agent"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hybrid"] == []
    diag = data["diagnostics"]
    assert diag["reason"] == "no_entities_of_allowed_domains"
    assert set(diag["allowed_domains"]) == {"climate", "sensor", "weather"}
    assert diag["domain_counts"] == {"climate": 0, "sensor": 0, "weather": 0}


@pytest.mark.asyncio
async def test_match_preview_diagnostics_filtered_out(preview_client):
    """Empty hybrid but allowed-domain entities exist -> reason=filtered_out."""
    client, ei, em = preview_client
    em.match.return_value = []
    ei._store = MagicMock()
    ei._store.get = MagicMock(
        return_value={
            "metadatas": [
                {"domain": "sensor"},
                {"domain": "sensor"},
                {"domain": "light"},
            ]
        }
    )
    resolver_output = {
        "entity_id": None,
        "friendly_name": "foo",
        "speech": None,
        "metadata": {"query": "foo", "resolution_path": "no_match", "match_count": 0},
    }
    with (
        patch(
            "app.entity.deterministic_resolver.resolve_entity_deterministic_first",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "foo", "agent_id": "climate-agent"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    diag = data["diagnostics"]
    assert diag["reason"] == "filtered_out"
    assert diag["domain_counts"]["sensor"] == 2
    assert "light" not in diag["domain_counts"]


@pytest.mark.asyncio
async def test_match_preview_diagnostics_absent_when_hybrid_nonempty(preview_client):
    """Diagnostics block must not appear when hybrid returns candidates."""
    client, *_ = preview_client
    resolver_output = {
        "entity_id": "light.keller",
        "friendly_name": "Keller",
        "speech": None,
        "metadata": {"query": "keller", "resolution_path": "exact_friendly_name", "match_count": 1},
    }
    with (
        patch(
            "app.entity.deterministic_resolver.resolve_entity_deterministic_first",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "keller", "agent_id": "light-agent"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "diagnostics" not in data


@pytest.mark.asyncio
async def test_match_preview_diagnostics_explicit_domain(preview_client):
    """Explicit ?domain= path: single-domain count map and matching reason."""
    client, ei, em = preview_client
    em.match.return_value = []
    ei._store = MagicMock()
    ei._store.get = MagicMock(return_value={"metadatas": [{"domain": "climate"}]})
    resolver_output = {
        "entity_id": None,
        "friendly_name": "foo",
        "speech": None,
        "metadata": {"query": "foo", "resolution_path": "no_match", "match_count": 0},
    }
    with (
        patch(
            "app.entity.deterministic_resolver.resolve_entity_deterministic_first",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "foo", "domain": "climate"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    diag = data["diagnostics"]
    assert diag["reason"] == "filtered_out"
    assert diag["allowed_domains"] == ["climate"]
    assert diag["domain_counts"] == {"climate": 1}


@pytest.mark.asyncio
async def test_match_preview_diagnostics_unknown_when_no_filters(preview_client):
    """No agent_id and no domain filter -> reason=unknown."""
    client, _ei, em = preview_client
    em.match.return_value = []
    resolver_output = {
        "entity_id": None,
        "friendly_name": "foo",
        "speech": None,
        "metadata": {"query": "foo", "resolution_path": "no_match", "match_count": 0},
    }
    with (
        patch(
            "app.agents.action_executor._resolve_light_entity",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "foo"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    diag = data["diagnostics"]
    assert diag["reason"] == "unknown"
    assert diag["allowed_domains"] == []
    assert diag["domain_counts"] == {}


@pytest.mark.asyncio
async def test_match_preview_diagnostics_with_hybrid_error(preview_client):
    """Diagnostics still attached when matcher errors out and hybrid is empty."""
    client, ei, em = preview_client
    em.match.side_effect = RuntimeError("boom")
    ei._store = MagicMock()
    ei._store.get = MagicMock(return_value={"metadatas": [{"domain": "climate"}]})
    resolver_output = {
        "entity_id": None,
        "friendly_name": "foo",
        "speech": None,
        "metadata": {"query": "foo", "resolution_path": "no_match", "match_count": 0},
    }
    with (
        patch(
            "app.agents.action_executor._resolve_light_entity",
            new=AsyncMock(return_value=resolver_output),
        ),
        patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(
            "/api/admin/entity-index/match-preview",
            params={"q": "foo", "agent_id": "climate-agent"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hybrid_error"] == "boom"
    assert data["diagnostics"]["reason"] == "filtered_out"
