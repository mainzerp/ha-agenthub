"""Admin sub-router: LLM provider management."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import litellm
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.api.routes import admin as _admin_pkg
from app.db.repository import SecretsRepository, SettingsRepository
from app.security.encryption import delete_secret, store_secret

logger = logging.getLogger(__name__)

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
    api_key: str
    extra_headers: dict[str, str] | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("base_url is required")
        low = v.lower()
        if not low.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        if len(v) > 64:
            raise ValueError("name must be at most 64 characters")
        return v


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
    await store_secret("custom_openai_api_key", payload.api_key)
    await SettingsRepository.set(
        "custom_openai_provider.name",
        payload.name,
        "string",
        "llm",
        "Custom OpenAI provider name",
    )
    await SettingsRepository.set(
        "custom_openai_provider.base_url",
        payload.base_url,
        "string",
        "llm",
        "Custom OpenAI provider base URL",
    )
    await SettingsRepository.set(
        "custom_openai_provider.headers",
        json.dumps(payload.extra_headers or {}),
        "json",
        "llm",
        "Custom OpenAI provider extra headers",
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


@router.post("/llm-providers/test")
async def test_llm_provider(payload: ProviderTestRequest):
    """Test connectivity for an LLM provider."""
    provider = payload.provider
    api_key = payload.api_key

    test_models = {
        "groq": "groq/llama-3.1-8b-instant",
        "openrouter": "openrouter/openai/gpt-4o-mini",
        "anthropic": "anthropic/claude-3-haiku-20240307",
        "cerebras": "cerebras/llama3.1-8b",
        "ollama": "ollama/llama3",
        "custom_openai": payload.model or "custom_openai/gpt-4o-mini",
    }
    if provider not in test_models:
        return {"status": "error", "detail": f"Unknown provider: {provider}"}

    # If no key given, retrieve stored key
    if provider == "ollama":
        base_url = await SettingsRepository.get_value("ollama_base_url", "http://localhost:11434")
        api_key = "not-needed"
    elif provider == "custom_openai":
        base_url = await SettingsRepository.get_value("custom_openai_provider.base_url")
        if not api_key:
            api_key = await _admin_pkg.retrieve_secret("custom_openai_api_key")
        if not base_url:
            return {"status": "error", "detail": "Custom OpenAI provider base URL not configured"}
        if not api_key:
            return {"status": "error", "detail": "Custom OpenAI provider API key not configured"}
    elif not api_key:
        secret_key = PROVIDER_SECRET_KEYS.get(provider)
        if secret_key:
            api_key = await _admin_pkg.retrieve_secret(secret_key)
        if not api_key:
            return {"status": "error", "detail": "No API key configured for " + provider}

    try:
        kwargs: dict[str, Any] = {
            "model": test_models[provider],
            "messages": [{"role": "user", "content": "Say hello"}],
            "api_key": api_key,
            "max_tokens": 10,
        }
        if provider == "ollama":
            kwargs["api_base"] = base_url
        elif provider == "custom_openai":
            kwargs["api_base"] = base_url
            headers_raw = await SettingsRepository.get_value("custom_openai_provider.headers", "{}")
            try:
                extra_headers = json.loads(headers_raw or "{}")
            except json.JSONDecodeError:
                extra_headers = {}
            if isinstance(extra_headers, dict) and extra_headers:
                kwargs["extra_headers"] = extra_headers
        await litellm.acompletion(**kwargs)
        return {"status": "ok", "provider": provider}
    except asyncio.CancelledError:
        raise
    except (litellm.exceptions.APIError, litellm.exceptions.AuthenticationError, OSError):
        logger.warning("LLM provider test failed for %s", provider, exc_info=True)
        return {"status": "error", "detail": "Provider test failed. Check server logs."}


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
