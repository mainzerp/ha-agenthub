"""Admin REST API endpoints.

This package replaces the former monolithic ``admin.py`` module. The public
import surface is preserved: ``app.api.routes.admin.router`` and
``app.api.routes.admin.set_registry`` keep working, and every external symbol
the original module exposed is re-exported here so that
``app.api.routes.admin.<name>`` keeps resolving (used by ``main.py`` and by
tests via ``mock.patch("app.api.routes.admin.<name>")``).

Each per-domain sub-router is a plain ``APIRouter()``; the combined ``router``
below applies the original ``/api/admin`` prefix, ``admin`` tag and the admin
auth/size-limit dependencies to every included route (FastAPI propagates the
parent router's prefix, tags and dependencies to ``include_router`` children).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

# External symbols re-exported to preserve the public/import surface so that
# ``app.api.routes.admin.<name>`` keeps resolving (used by main.py and tests).
from app.db.repository import (
    AgentConfigRepository,
    EntityMatchingConfigRepository,
    EntityVisibilityRepository,
    ScheduledTimersRepository,
    SecretsRepository,
    SettingsRepository,
)
from app.ha_client.auth import get_ha_token, set_ha_token
from app.ha_client.rest import test_ha_connection
from app.security.auth import API_KEY_SECRET_NAME, body_size_limit, require_admin_session
from app.security.encryption import delete_secret, retrieve_secret, store_secret

from ._agents import router as _agents_router
from ._ha_connection import (
    HaConnectionTestRequest,
    HaConnectionUpdate,
)
from ._ha_connection import (
    router as _ha_connection_router,
)
from ._llm_providers import (
    PROVIDER_SECRET_KEYS,
    CustomProviderConfig,
    OllamaUrlUpdate,
    ProviderKeyUpdate,
    ProviderTestRequest,
)
from ._llm_providers import (
    router as _llm_providers_router,
)
from ._misc import (
    ContainerApiKeySetPayload,
    FernetKeyBackupPayload,
)
from ._misc import (
    router as _misc_router,
)
from ._settings import (
    SettingsUpdatePayload,
    WakeBriefingSettingsPayload,
    WakeBriefingSourcesPayload,
    _validate_setting_value,
)
from ._settings import (
    router as _settings_router,
)
from ._shared import _reload_ha_clients_after_settings_change, set_registry
from ._timers import (
    AlarmRecurrencePayload,
    TimerCreatePayload,
    TimerPatchPayload,
)
from ._timers import (
    router as _timers_router,
)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_session), Depends(body_size_limit(10 * 1024 * 1024))],
)

router.include_router(_settings_router)
router.include_router(_llm_providers_router)
router.include_router(_ha_connection_router)
router.include_router(_timers_router)
router.include_router(_agents_router)
router.include_router(_misc_router)

__all__ = [
    "API_KEY_SECRET_NAME",
    "PROVIDER_SECRET_KEYS",
    "AgentConfigRepository",
    "AlarmRecurrencePayload",
    "ContainerApiKeySetPayload",
    "CustomProviderConfig",
    "EntityMatchingConfigRepository",
    "EntityVisibilityRepository",
    "FernetKeyBackupPayload",
    "HaConnectionTestRequest",
    "HaConnectionUpdate",
    "OllamaUrlUpdate",
    "ProviderKeyUpdate",
    "ProviderTestRequest",
    "ScheduledTimersRepository",
    "SecretsRepository",
    "SettingsRepository",
    "SettingsUpdatePayload",
    "TimerCreatePayload",
    "TimerPatchPayload",
    "WakeBriefingSettingsPayload",
    "WakeBriefingSourcesPayload",
    "_reload_ha_clients_after_settings_change",
    "_validate_setting_value",
    "delete_secret",
    "get_ha_token",
    "retrieve_secret",
    "router",
    "set_ha_token",
    "set_registry",
    "store_secret",
    "test_ha_connection",
]
