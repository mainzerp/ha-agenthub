"""Sidebar navigation configuration."""

NAV_GROUPS = [
    {
        "key": "operate",
        "label": "Operate",
        "items": [
            {"key": "overview", "label": "Overview", "href": "/dashboard/", "icon_id": "overview"},
            {"key": "chat", "label": "Chat", "href": "/dashboard/chat", "icon_id": "chat"},
            {"key": "system_health", "label": "System Health", "href": "/dashboard/system-health", "icon_id": "heart"},
            {"key": "analytics", "label": "Analytics", "href": "/dashboard/analytics", "icon_id": "bar_chart"},
            {"key": "traces", "label": "Traces", "href": "/dashboard/traces", "icon_id": "activity"},
            {"key": "logs", "label": "Logs", "href": "/dashboard/logs", "icon_id": "list"},
        ],
    },
    {
        "key": "configure",
        "label": "Configure",
        "items": [
            {"key": "agents", "label": "Agents", "href": "/dashboard/agents", "icon_id": "users"},
            {
                "key": "custom_agents",
                "label": "Custom Agents",
                "href": "/dashboard/custom-agents",
                "icon_id": "user_plus",
            },
            {"key": "personality", "label": "Personality", "href": "/dashboard/personality", "icon_id": "smile"},
            {"key": "entity_index", "label": "Entity Index", "href": "/dashboard/entity-index", "icon_id": "database"},
            {"key": "mcp_servers", "label": "MCP Servers", "href": "/dashboard/mcp-servers", "icon_id": "server"},
            {"key": "plugins", "label": "Plugins", "href": "/dashboard/plugins", "icon_id": "zap"},
        ],
    },
    {
        "key": "domain_data",
        "label": "Domain Data",
        "items": [
            {"key": "send_devices", "label": "Send Devices", "href": "/dashboard/send-devices", "icon_id": "send"},
            {"key": "timers", "label": "Timers", "href": "/dashboard/timers", "icon_id": "clock"},
            {"key": "calendar", "label": "Calendar", "href": "/dashboard/calendar", "icon_id": "calendar"},
            {"key": "persons", "label": "Persons", "href": "/dashboard/persons", "icon_id": "user"},
        ],
    },
    {
        "key": "performance",
        "label": "Performance",
        "items": [
            {"key": "cache", "label": "Cache", "href": "/dashboard/cache", "icon_id": "layers"},
        ],
    },
    {
        "key": "system",
        "label": "System",
        "items": [
            {"key": "settings", "label": "Settings", "href": "/dashboard/settings", "icon_id": "settings"},
        ],
    },
]
