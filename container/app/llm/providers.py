import json

from app.db.repository import SettingsRepository
from app.security.encryption import retrieve_secret

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


async def resolve_provider_params(model: str) -> dict:
    provider = extract_provider(model)
    params: dict = {}
    api_key = await get_api_key(provider)
    if api_key is not None:
        params["api_key"] = api_key
    base_url = await get_base_url(provider)
    if base_url is not None:
        params["api_base"] = base_url
    if provider == "custom_openai":
        headers_raw = await SettingsRepository.get_value("custom_openai_provider.headers", "{}")
        try:
            extra_headers = json.loads(headers_raw or "{}")
        except json.JSONDecodeError:
            extra_headers = {}
        if isinstance(extra_headers, dict) and extra_headers:
            params["extra_headers"] = extra_headers
    return params
