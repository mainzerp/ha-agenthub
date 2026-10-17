"""Admin sub-router: LLM provider management."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import litellm
import openai
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.api.routes import admin as _admin_pkg
from app.db.repository import SecretsRepository, SettingsRepository
from app.llm import model_catalog
from app.llm.client import _is_timeout_error
from app.llm.providers import (
    get_custom_openai_headers,
    normalize_custom_openai_base_url,
    normalize_custom_openai_name,
    save_custom_openai_config,
)
from app.security.encryption import delete_secret, store_secret

logger = logging.getLogger(__name__)

# Upper bound for the custom OpenAI "test connection" completion fallback. Passed to litellm as
# the provider timeout; ``asyncio.wait_for`` adds a small grace period as a hard
# stop in case a provider adapter ignores the per-call timeout.
_PROVIDER_TEST_TIMEOUT_S = 30.0
_PROVIDER_TEST_GRACE_S = 5.0

# Maps provider name to its secret key in the secrets table
PROVIDER_SECRET_KEYS = {
    "openrouter": "openrouter_api_key",
    "groq": "groq_api_key",
    "anthropic": "anthropic_api_key",
    "cerebras": "cerebras_api_key",
    "custom_openai": "custom_openai_api_key",
}


class ProviderKeyUpdate(BaseModel):
    provider: str
    api_key: str


class OllamaUrlUpdate(BaseModel):
    url: str


class ProviderTestRequest(BaseModel):
    provider: str
    api_key: str | None = None
    model: str | None = None


class CustomProviderConfig(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    extra_headers: dict[str, str] | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        return normalize_custom_openai_base_url(v)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return normalize_custom_openai_name(v)


router = APIRouter()


@router.get("/llm-providers")
async def get_llm_provider_status():
    """Return status of all LLM providers with masked keys."""
    stored_keys = await SecretsRepository.list_keys()
    providers: dict = {}
    custom_url = await SettingsRepository.get_value("custom_openai_provider.base_url")
    for provider, secret_key in PROVIDER_SECRET_KEYS.items():
        if provider == "custom_openai":
            configured = secret_key in stored_keys and custom_url is not None
        else:
            configured = secret_key in stored_keys
        providers[provider] = {"configured": configured}
    # Ollama
    ollama_url = await SettingsRepository.get_value("ollama_base_url")
    providers["ollama"] = {
        "configured": ollama_url is not None,
        "url": ollama_url,
    }
    # Custom OpenAI
    custom_name = await SettingsRepository.get_value("custom_openai_provider.name")
    providers["custom_openai"].update(
        {
            "name": custom_name,
            "url": custom_url,
        }
    )
    return {"providers": providers}


@router.put("/llm-providers")
async def update_llm_provider_key(payload: ProviderKeyUpdate):
    """Save an encrypted API key for a provider."""
    if payload.provider not in PROVIDER_SECRET_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {payload.provider}")
    secret_key = PROVIDER_SECRET_KEYS[payload.provider]
    await store_secret(secret_key, payload.api_key)
    return {"status": "ok", "provider": payload.provider}


@router.put("/llm-providers/custom-openai")
async def update_custom_openai_config(payload: CustomProviderConfig):
    """Save custom OpenAI-compatible provider configuration."""
    await save_custom_openai_config(
        name=payload.name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        extra_headers=payload.extra_headers,
    )
    return {"status": "ok", "provider": "custom_openai"}


@router.put("/llm-providers/ollama")
async def update_ollama_url(payload: OllamaUrlUpdate):
    """Save the Ollama base URL."""
    await SettingsRepository.set("ollama_base_url", payload.url, "string", "llm", "Ollama API URL")
    return {"status": "ok"}


@router.delete("/llm-providers/{provider}")
async def delete_llm_provider_key(provider: str):
    """Remove a stored API key for a provider."""
    if provider not in PROVIDER_SECRET_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    await delete_secret(PROVIDER_SECRET_KEYS[provider])
    if provider == "custom_openai":
        await SettingsRepository.delete("custom_openai_provider.name")
        await SettingsRepository.delete("custom_openai_provider.base_url")
        await SettingsRepository.delete("custom_openai_provider.headers")
    return {"status": "ok"}


def _test_error(kind: str, detail: str) -> dict[str, Any]:
    return {"status": "error", "error_kind": kind, "detail": detail}


def _classify_provider_exception(provider: str, exc: BaseException) -> dict[str, Any] | None:
    """Map a provider/network exception to a test error result, or ``None`` if it is not one."""
    # Checked first: builtin TimeoutError is an OSError subclass, and
    # litellm's Timeout is an openai.APIConnectionError subclass.
    if _is_timeout_error(exc):
        logger.warning("LLM provider test timed out for %s", provider)
        return _test_error(
            model_catalog.ERROR_UNREACHABLE,
            f"Provider test timed out after {_PROVIDER_TEST_TIMEOUT_S:.0f}s.",
        )
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        logger.warning("LLM provider test failed for %s", provider, exc_info=True)
        return _test_error(model_catalog.ERROR_AUTH, "Authentication failed. Check the API key.")
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError | OSError):
        logger.warning("LLM provider test failed for %s", provider, exc_info=True)
        return _test_error(model_catalog.ERROR_UNREACHABLE, "Could not connect to the provider.")
    if isinstance(exc, openai.APIError | httpx.HTTPError):
        logger.warning("LLM provider test failed for %s", provider, exc_info=True)
        return _test_error(model_catalog.ERROR_PROVIDER, "Provider test failed. Check server logs.")
    return None


async def _test_custom_openai_completion(model: str, api_key: str | None) -> dict[str, Any]:
    """Fallback test for custom endpoints without ``/models``: one tiny completion."""
    base_url = await SettingsRepository.get_value("custom_openai_provider.base_url")
    if not api_key:
        api_key = await _admin_pkg.retrieve_secret("custom_openai_api_key")
    if not base_url:
        return _test_error(model_catalog.ERROR_NOT_CONFIGURED, "Custom OpenAI provider base URL not configured")
    if not api_key:
        return _test_error(model_catalog.ERROR_NOT_CONFIGURED, "Custom OpenAI provider API key not configured")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello"}],
        "api_key": api_key,
        "api_base": base_url,
        "max_tokens": 10,
        "timeout": _PROVIDER_TEST_TIMEOUT_S,
    }
    extra_headers = await get_custom_openai_headers()
    if extra_headers:
        kwargs["extra_headers"] = extra_headers
    await asyncio.wait_for(
        litellm.acompletion(**kwargs),
        timeout=_PROVIDER_TEST_TIMEOUT_S + _PROVIDER_TEST_GRACE_S,
    )
    return {
        "status": "ok",
        "detail": f"Connection OK, completion with {model} succeeded (model listing not supported).",
        "model_count": None,
    }


@router.post("/llm-providers/test")
async def test_llm_provider(payload: ProviderTestRequest):
    """Validate a provider's credentials by listing its models.

    Provider and network failures are always reported as
    ``{"status": "error", ...}`` with HTTP 200, never as a 500.
    """
    provider = payload.provider
    if provider not in model_catalog.CATALOG_PROVIDERS:
        return _test_error(model_catalog.ERROR_UNSUPPORTED, f"Unknown provider: {provider}")
    api_key = (payload.api_key or "").strip() or None

    try:
        result = await model_catalog.list_provider_models(provider, api_key=api_key, use_cache=False)
        if result["status"] == "ok":
            count = len(result["models"])
            return {"status": "ok", "detail": f"Connection OK, {count} models available.", "model_count": count}
        kind = result["error_kind"]
        if provider == "custom_openai" and kind == model_catalog.ERROR_UNSUPPORTED:
            model = (payload.model or "").strip()
            if model:
                # litellm routes the completion by the provider prefix; accept bare names too.
                if not model.startswith("custom_openai/"):
                    model = f"custom_openai/{model}"
                return await _test_custom_openai_completion(model, api_key)
            return _test_error(kind, result["detail"] + " Enter a model name to test with a completion instead.")
        return _test_error(kind, result["detail"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        classified = _classify_provider_exception(provider, exc)
        if classified is None:
            raise
        return classified


@router.get("/llm-providers/{provider}/models")
async def list_llm_provider_models(provider: str, refresh: bool = False):
    """List the models a provider offers (cached; ``refresh=true`` refetches).

    Unknown providers return 404; provider-side failures return HTTP 200
    with ``status: "error"``.
    """
    if provider not in model_catalog.CATALOG_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    try:
        return await model_catalog.list_provider_models(provider, use_cache=not refresh)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        classified = _classify_provider_exception(provider, exc)
        if classified is None:
            raise
        return {"provider": provider, **classified, "models": []}


@router.get("/llm-providers/configured")
async def get_configured_providers():
    """Return all known providers with their configuration status."""
    stored_keys = await SecretsRepository.list_keys()
    configured = []
    all_providers = []
    custom_url = await SettingsRepository.get_value("custom_openai_provider.base_url")
    for provider, secret_key in PROVIDER_SECRET_KEYS.items():
        all_providers.append(provider)
        if provider == "custom_openai":
            if secret_key in stored_keys and custom_url:
                configured.append(provider)
        elif secret_key in stored_keys:
            configured.append(provider)
    all_providers.append("ollama")
    ollama_url = await SettingsRepository.get_value("ollama_base_url")
    if ollama_url:
        configured.append("ollama")
    return {"providers": all_providers, "configured": configured}
