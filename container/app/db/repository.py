"""Re-export shim for backward compatibility.

All repository classes and helpers now live under ``app.db.repositories.*``.
Import from ``app.db.repository`` is still supported via this shim.
"""

from app.db.repositories import (  # noqa: F401
    AdminAccountRepository,
    AgentConfigRepository,
    AgentMcpToolsRepository,
    AliasRepository,
    AnalyticsRepository,
    CacheValidatorRepository,
    CalendarEntitySettingsRepository,
    CalendarReminderStateRepository,
    CalendarUserMappingRepository,
    ConversationRepository,
    CustomAgentRepository,
    EntityMatchingConfigRepository,
    EntityVisibilityRepository,
    McpServerRepository,
    PluginRepository,
    QuerySynonymCacheRepository,
    ScheduledTimersRepository,
    SecretsRepository,
    SendDeviceMappingRepository,
    SettingsRepository,
    SetupStateRepository,
    TraceSpanRepository,
    TraceSummaryRepository,
    _normalize_device_name,
    _now,
    _phonetic_key,
    _validate_column_name,
    custom_agent_id_for_name,
    normalize_custom_agent_name,
)
from app.db.schema import get_db_read, get_db_write  # noqa: F401
