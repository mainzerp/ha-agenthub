import json

from app.db.repository import SettingsRepository
from app.security.encryption import retrieve_secret, store_secret
from app.util.memoize import AsyncTtlCache

# Maps the provider prefix (extracted from model string) to the secret key
# stored in the `secrets` table.
PROVIDER_SECRET_MAP: dict[str, str] = {
    "openrouter": "openrouter_api_key",
    "groq": "groq_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "cerebras": "cerebras_api_key",
    "custom_openai": "custom_openai_api_key",
}

# Providers that run locally and do not need an API key.
LOCAL_PROVIDERS: set[str] = {"ollama"}

# Settings key for the Ollama base URL.
OLLAMA_BASE_URL_KEY = "ollama_base_url"
OLLAMA_BASE_URL_DEFAULT = "http://localhost:11434"

# Settings keys that feed ``resolve_provider_params``; a write to any of
# them must invalidate the memoized params.
_PROVIDER_PARAMS_SETTINGS_KEYS: frozenset[str] = frozenset(
    {
        OLLAMA_BASE_URL_KEY,
        "custom_openai_provider.base_url",
        "custom_openai_provider.headers",
    }
)

# P2: memoize resolved provider params per model on the LLM hot path
# (Fernet decrypt + settings reads per call before). Invalidated on any
# secret write (encryption.store_secret/delete_secret) and on writes to
# the settings keys above; bounded by a TTL as a safety net.
_PARAMS_CACHE_TTL_SEC = 60.0
_params_cache = AsyncTtlCache(_PARAMS_CACHE_TTL_SEC)


async def invalidate_provider_params() -> None:
    """Drop all memoized provider params (called on secret/settings writes)."""
    await _params_cache.invalidate()


async def _on_settings_write(key: str) -> None:
    if key in _PROVIDER_PARAMS_SETTINGS_KEYS:
        await invalidate_provider_params()


SettingsRepository.register_write_listener(_on_settings_write)


def extract_provider(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[0]
    return "openai"


async def get_api_key(provider: str) -> str | None:
    if provider in LOCAL_PROVIDERS:
        return None
    secret_key = PROVIDER_SECRET_MAP.get(provider)
    if secret_key is None:
        return None
    return await retrieve_secret(secret_key)


async def get_base_url(provider: str) -> str | None:
    if provider == "ollama":
        return await SettingsRepository.get_value(OLLAMA_BASE_URL_KEY, OLLAMA_BASE_URL_DEFAULT)
    if provider == "custom_openai":
        return await SettingsRepository.get_value("custom_openai_provider.base_url")
    return None


async def get_custom_openai_headers() -> dict[str, str]:
    """Return the stored extra headers for the custom OpenAI provider ({} when unset or invalid)."""
    headers_raw = await SettingsRepository.get_value("custom_openai_provider.headers", "{}")
    try:
        extra_headers = json.loads(headers_raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(extra_headers, dict):
        return {}
    return {str(k): str(v) for k, v in extra_headers.items()}


CUSTOM_OPENAI_SECRET_KEY = "custom_openai_api_key"
CUSTOM_OPENAI_NAME_MAX_LEN = 64


def normalize_custom_openai_name(value: str | None) -> str:
    """Strip and validate a custom OpenAI provider display name (raises ``ValueError``)."""
    value = (value or "").strip()
    if not value:
        raise ValueError("name is required")
    if len(value) > CUSTOM_OPENAI_NAME_MAX_LEN:
        raise ValueError(f"name must be at most {CUSTOM_OPENAI_NAME_MAX_LEN} characters")
    return value


def normalize_custom_openai_base_url(value: str | None) -> str:
    """Strip and validate a custom OpenAI provider base URL (raises ``ValueError``)."""
    value = (value or "").strip()
    if not value:
        raise ValueError("base_url is required")
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return value


async def save_custom_openai_config(
    *,
    name: str,
    base_url: str,
    api_key: str = "",
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Persist the custom OpenAI-compatible provider configuration.

    ``name`` and ``base_url`` must already be normalized. An empty ``api_key``
    keeps the stored key; ``extra_headers=None`` keeps the stored headers.
    Cache invalidation is implicit: ``store_secret`` drops the provider params
    and model catalog caches, and the settings write listeners do the same for
    the ``custom_openai_provider.*`` keys.
    """
    if api_key:
        await store_secret(CUSTOM_OPENAI_SECRET_KEY, api_key)
    await SettingsRepository.set(
        "custom_openai_provider.name",
        name,
        "string",
        "llm",
        "Custom OpenAI provider name",
    )
    await SettingsRepository.set(
        "custom_openai_provider.base_url",
        base_url,
        "string",
        "llm",
        "Custom OpenAI provider base URL",
    )
    if extra_headers is not None:
        await SettingsRepository.set(
            "custom_openai_provider.headers",
            json.dumps(extra_headers),
            "json",
            "llm",
            "Custom OpenAI provider extra headers",
        )


async def resolve_provider_params(model: str) -> dict:
    hit, cached = await _params_cache.get(model)
    if hit:
        # Defensive copy: callers pass the dict into litellm kwargs.
        return dict(cached)
    provider = extract_provider(model)
    params: dict = {}
    api_key = await get_api_key(provider)
    if api_key is not None:
        params["api_key"] = api_key
    base_url = await get_base_url(provider)
    if base_url is not None:
        params["api_base"] = base_url
    if provider == "custom_openai":
        extra_headers = await get_custom_openai_headers()
        if extra_headers:
            params["extra_headers"] = extra_headers
    await _params_cache.put(model, params)
    return dict(params)
