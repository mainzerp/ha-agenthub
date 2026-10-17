"""Per-provider model catalog.

Lists the models a configured LLM provider offers by calling the provider's
own model-listing HTTP API. The admin "test connection" endpoint uses the
same call to validate credentials, so no completion against a hardcoded
model is needed.

Every result is a plain dict in one of two shapes:

- success: ``{"provider", "status": "ok", "models": [...], "cached", "fetched_at"}``
- failure: ``{"provider", "status": "error", "error_kind", "detail", "models": []}``

Each model item is ``{"id", "name", "model"}`` where ``model`` is the
litellm model string the agents store (``"<provider>/<id>"``).

Successful results fetched with the stored credentials are cached per
provider for ``_CACHE_TTL_SEC``. The cache is invalidated when a provider
secret is stored or deleted (``app.security.encryption``) and when the
Ollama / custom OpenAI settings change (settings write listener below).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.db.repository import SettingsRepository
from app.llm.providers import (
    OLLAMA_BASE_URL_KEY,
    PROVIDER_SECRET_MAP,
    get_api_key,
    get_base_url,
    get_custom_openai_headers,
)
from app.util.memoize import AsyncTtlCache

logger = logging.getLogger(__name__)

# Providers the admin UI manages, in display order.
CATALOG_PROVIDERS: tuple[str, ...] = ("openrouter", "groq", "anthropic", "cerebras", "custom_openai", "ollama")

# Error kinds reported in failure results.
ERROR_NOT_CONFIGURED = "not_configured"
ERROR_AUTH = "auth"
ERROR_UNSUPPORTED = "unsupported"
ERROR_UNREACHABLE = "unreachable"
ERROR_PROVIDER = "provider_error"

_HTTP_TIMEOUT_S = 15.0
_CACHE_TTL_SEC = 3600.0
# Safety bound for paginated listings (Anthropic returns up to 1000 per page).
_MAX_PAGES = 20

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
_CEREBRAS_MODELS_URL = "https://api.cerebras.ai/v1/models"
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_OPENROUTER_KEY_FALLBACK_URL = "https://openrouter.ai/api/v1/auth/key"

_catalog_cache = AsyncTtlCache(_CACHE_TTL_SEC)

# Maps a secret name to the catalog provider whose cache it feeds.
_SECRET_TO_PROVIDER: dict[str, str] = {
    secret: provider for provider, secret in PROVIDER_SECRET_MAP.items() if provider in CATALOG_PROVIDERS
}


class ModelCatalogError(Exception):
    """A classified, user-presentable failure while listing models."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


async def invalidate_model_catalog(provider: str | None = None) -> None:
    """Drop the cached model list for one provider, or for all when ``provider`` is ``None``."""
    await _catalog_cache.invalidate(provider)


async def invalidate_for_secret(secret_key: str) -> None:
    """Drop the cached model list for the provider that uses ``secret_key``."""
    provider = _SECRET_TO_PROVIDER.get(secret_key)
    if provider is not None:
        await invalidate_model_catalog(provider)


async def _on_settings_write(key: str) -> None:
    if key == OLLAMA_BASE_URL_KEY:
        await invalidate_model_catalog("ollama")
    elif key.startswith("custom_openai_provider."):
        await invalidate_model_catalog("custom_openai")


SettingsRepository.register_write_listener(_on_settings_write)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _error_result(provider: str, kind: str, detail: str) -> dict[str, Any]:
    return {"provider": provider, "status": "error", "error_kind": kind, "detail": detail, "models": []}


def _item(provider: str, model_id: str, name: Any = None) -> dict[str, str]:
    display = name if isinstance(name, str) and name.strip() else model_id
    return {"id": model_id, "name": display, "model": f"{provider}/{model_id}"}


def _check_status(resp: httpx.Response) -> None:
    """Raise a classified error for a non-2xx response (never includes credentials)."""
    if resp.is_success:
        return
    code = resp.status_code
    if code in (401, 403):
        raise ModelCatalogError(ERROR_AUTH, f"Authentication failed (HTTP {code}). Check the API key.")
    raise ModelCatalogError(ERROR_PROVIDER, f"Provider returned HTTP {code}.")


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        raise ModelCatalogError(ERROR_PROVIDER, "Provider returned a malformed response.") from None


def _data_list(body: Any, field: str = "data") -> list[Any]:
    if not isinstance(body, dict) or not isinstance(body.get(field), list):
        raise ModelCatalogError(ERROR_PROVIDER, "Provider returned an unexpected response format.")
    return body[field]


def _openai_style_items(provider: str, body: Any) -> list[dict[str, str]]:
    items = []
    for entry in _data_list(body):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
            items.append(_item(provider, entry["id"], entry.get("name")))
    return sorted(items, key=lambda item: item["id"])


async def _resolve_key(provider: str, api_key: str | None) -> str:
    if api_key:
        return api_key
    try:
        stored = await get_api_key(provider)
    except RuntimeError:
        raise ModelCatalogError(
            ERROR_NOT_CONFIGURED, f"Stored API key for {provider} could not be decrypted. Save it again."
        ) from None
    if not stored:
        raise ModelCatalogError(ERROR_NOT_CONFIGURED, f"No API key configured for {provider}.")
    return stored


async def _list_anthropic(client: httpx.AsyncClient, api_key: str) -> list[dict[str, str]]:
    headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
    params: dict[str, Any] = {"limit": 1000}
    items: list[dict[str, str]] = []
    for _ in range(_MAX_PAGES):
        resp = await client.get(_ANTHROPIC_MODELS_URL, headers=headers, params=params)
        _check_status(resp)
        body = _json(resp)
        for entry in _data_list(body):
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                items.append(_item("anthropic", entry["id"], entry.get("display_name")))
        last_id = body.get("last_id")
        if not body.get("has_more") or not isinstance(last_id, str) or not last_id:
            break
        params = {"limit": 1000, "after_id": last_id}
    # Keep API order (newest first).
    return items


async def _list_bearer(client: httpx.AsyncClient, provider: str, url: str, api_key: str) -> list[dict[str, str]]:
    resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    _check_status(resp)
    return _openai_style_items(provider, _json(resp))


async def _list_openrouter(client: httpx.AsyncClient, api_key: str) -> list[dict[str, str]]:
    # The model list is public, so validate the key explicitly first.
    auth = {"Authorization": f"Bearer {api_key}"}
    resp = await client.get(_OPENROUTER_KEY_URL, headers=auth)
    if resp.status_code == 404:
        resp = await client.get(_OPENROUTER_KEY_FALLBACK_URL, headers=auth)
    _check_status(resp)
    resp = await client.get(_OPENROUTER_MODELS_URL, headers=auth)
    _check_status(resp)
    return _openai_style_items("openrouter", _json(resp))


async def _list_ollama(client: httpx.AsyncClient, base_url_override: str | None) -> list[dict[str, str]]:
    base_url = base_url_override or await get_base_url("ollama")
    if not base_url:
        raise ModelCatalogError(ERROR_NOT_CONFIGURED, "Ollama base URL not configured.")
    resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
    _check_status(resp)
    items = []
    for entry in _data_list(_json(resp), "models"):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
            items.append(_item("ollama", entry["name"]))
    return sorted(items, key=lambda item: item["id"])


async def _list_custom_openai(
    client: httpx.AsyncClient, api_key: str | None, base_url_override: str | None
) -> list[dict[str, str]]:
    stored_url = await get_base_url("custom_openai")
    base_url = base_url_override or stored_url
    if not base_url:
        raise ModelCatalogError(ERROR_NOT_CONFIGURED, "Custom OpenAI provider base URL not configured.")
    # Stored credentials and headers belong to the stored endpoint; never send them to another host.
    use_stored_credentials = base_url == stored_url
    if not api_key and use_stored_credentials:
        try:
            api_key = await get_api_key("custom_openai")
        except RuntimeError:
            raise ModelCatalogError(
                ERROR_NOT_CONFIGURED, "Stored API key for custom_openai could not be decrypted. Save it again."
            ) from None
    headers = dict(await get_custom_openai_headers()) if use_stored_credentials else {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
    if resp.status_code in (404, 405):
        raise ModelCatalogError(
            ERROR_UNSUPPORTED,
            f"The custom endpoint does not support listing models (HTTP {resp.status_code}).",
        )
    _check_status(resp)
    return _openai_style_items("custom_openai", _json(resp))


async def _fetch(provider: str, api_key: str | None, base_url: str | None) -> list[dict[str, str]]:
    # Resolve credentials before opening a client so not_configured never touches the network.
    key: str | None = None
    if provider in ("anthropic", "groq", "cerebras", "openrouter"):
        key = await _resolve_key(provider, api_key)
    async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_S)) as client:
        if provider == "anthropic":
            return await _list_anthropic(client, key or "")
        if provider == "groq":
            return await _list_bearer(client, "groq", _GROQ_MODELS_URL, key or "")
        if provider == "cerebras":
            return await _list_bearer(client, "cerebras", _CEREBRAS_MODELS_URL, key or "")
        if provider == "openrouter":
            return await _list_openrouter(client, key or "")
        if provider == "ollama":
            return await _list_ollama(client, base_url)
        return await _list_custom_openai(client, api_key, base_url)


async def list_provider_models(
    provider: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """List the models ``provider`` offers, as a normalized result dict.

    ``api_key`` overrides the stored key (used to validate a key before it
    is saved); ``base_url`` overrides the stored Ollama / custom OpenAI base
    URL (the custom provider then skips the stored key and headers unless the
    URL matches the stored one). Results fetched with an explicit key or base
    URL are never cached.
    ``use_cache=False`` bypasses the cache but still refreshes it on a
    successful fetch with the stored credentials. Provider and network
    failures are returned as ``status: "error"`` results, never raised.
    """
    if provider not in CATALOG_PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    api_key = (api_key or "").strip() or None
    base_url = (base_url or "").strip() or None
    use_stored = api_key is None and base_url is None

    if use_cache and use_stored:
        hit, cached = await _catalog_cache.get(provider)
        if hit:
            return {**cached, "models": [dict(m) for m in cached["models"]], "cached": True}

    try:
        models = await _fetch(provider, api_key, base_url)
    except ModelCatalogError as exc:
        return _error_result(provider, exc.kind, exc.detail)
    except httpx.TimeoutException:
        logger.warning("Model listing timed out for %s", provider)
        return _error_result(provider, ERROR_UNREACHABLE, f"Provider did not respond within {_HTTP_TIMEOUT_S:.0f}s.")
    except httpx.UnsupportedProtocol:
        return _error_result(provider, ERROR_PROVIDER, "Configured base URL is invalid.")
    except (httpx.TransportError, OSError):
        logger.warning("Model listing could not reach %s", provider, exc_info=True)
        return _error_result(provider, ERROR_UNREACHABLE, "Could not connect to the provider.")
    except (httpx.HTTPError, httpx.InvalidURL):
        logger.warning("Model listing failed for %s", provider, exc_info=True)
        return _error_result(provider, ERROR_PROVIDER, "Model listing failed. Check server logs.")

    result: dict[str, Any] = {
        "provider": provider,
        "status": "ok",
        "models": models,
        "cached": False,
        "fetched_at": _utc_now_iso(),
    }
    if use_stored:
        await _catalog_cache.put(provider, {**result, "models": [dict(m) for m in models]})
    return result
