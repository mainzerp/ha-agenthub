#!/usr/bin/env python3
"""
Systematically replace inline style="..." attributes with CSS utility classes.
Uses precise string replacements per file.
"""
from pathlib import Path

TEMPLATES_DIR = Path("container/app/dashboard/templates")


def replace_in_file(filepath: Path, replacements: list):
    """Apply a list of (old_str, new_str) replacements to a file."""
    content = filepath.read_text(encoding="utf-8")
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new, 1)
        else:
            print(f"  WARNING: pattern not found in {filepath.name}")
    filepath.write_text(content, encoding="utf-8")


def count_styles(filepath: Path) -> int:
    import re
    content = filepath.read_text(encoding="utf-8")
    count = 0
    for match in re.finditer(r'<[^>]+style="([^"]*)"[^>]*>', content):
        tag = match.group(0)
        style = match.group(1)
        # Skip x-show + display:none
        if 'x-show' in tag and 'display:none' in style.replace(' ', ''):
            continue
        # Skip dynamic agentColor
        if 'agentColor(' in style:
            continue
        # Skip gantt container height
        if 'min-height: 200px' in style and 'gantt-container' in tag:
            continue
        count += 1
    return count


def main():
    # ================================================================
    # mcp_servers.html (1 -> 0)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "mcp_servers.html", [
        ('<td class="text-mono truncate" style="max-width:300px;" x-text="server.command_or_url"></td>',
         '<td class="text-mono truncate max-w-300" x-text="server.command_or_url"></td>'),
    ])

    # ================================================================
    # plugins.html (2 -> 0)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "plugins.html", [
        ('<span class="text-muted" style="margin-left: 1rem;">File:</span>',
         '<span class="text-muted ml-md">File:</span>'),
        ('<span class="text-muted" style="margin-left: 1rem;">Loaded:</span>',
         '<span class="text-muted ml-md">Loaded:</span>'),
    ])

    # ================================================================
    # custom_agents.html (2 -> 0)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "custom_agents.html", [
        ('<span class="text-muted" style="margin-left: 1rem;">Patterns:</span>',
         '<span class="text-muted ml-md">Patterns:</span>'),
        ('<span class="text-muted" style="margin-left: 1rem;">MCP Tools:</span>',
         '<span class="text-muted ml-md">MCP Tools:</span>'),
    ])

    # ================================================================
    # chat.html (3 -> 2: keep margin:auto and :style)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "chat.html", [
        ('<span class="text-mono text-muted" style="font-size: 0.75rem;" x-show="conversationId" x-text="\'Conv: \' + (conversationId || \'\').substring(0, 8) + \'...\'"></span>',
         '<span class="text-mono text-muted text-xs" x-show="conversationId" x-text="\'Conv: \' + (conversationId || \'\').substring(0, 8) + \'...\'"></span>'),
    ])

    # ================================================================
    # overview.html (4 -> 2)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "overview.html", [
        ('<div class="grid-2" style="margin-bottom: 1.5rem;">',
         '<div class="grid-2 mb-lg">'),
        ('<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">',
         '<div class="flex-between" style="margin-bottom: 0.75rem;">'),
        ('<p class="text-muted" style="padding: 1rem 0;">No recent traces.</p>',
         '<p class="text-muted pt-md pb-md">No recent traces.</p>'),
    ])

    # ================================================================
    # send_devices.html (4 -> 2)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "send_devices.html", [
        ('<div class="form-group" style="flex:1;min-width:180px;">',
         '<div class="form-group flex-1 min-w-180">'),
        ('<div class="form-group" style="flex: 1; min-width: 220px;">',
         '<div class="form-group flex-1 min-w-220">'),
        ('<label class="form-label" for="send-device-target-manual" style="font-size: 0.8125rem; margin-top: 0.5rem;">Or type a target manually</label>',
         '<label class="form-label mt-sm" for="send-device-target-manual" style="font-size: 0.8125rem;">Or type a target manually</label>'),
        ('<input id="send-device-target-manual" type="text" x-model="newMapping.ha_service_target"\n                           placeholder="or type manually" class="form-input" class="mt-sm">',
         '<input id="send-device-target-manual" type="text" x-model="newMapping.ha_service_target"\n                           placeholder="or type manually" class="form-input mt-sm">'),
    ])

    # ================================================================
    # system_health.html (6 -> 2)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "system_health.html", [
        ('<div class="flex-center" style="gap: 0.5rem; margin-bottom: 0.5rem;">',
         '<div class="flex-center gap-sm mb-sm">'),
        ('<div class="flex-between" style="margin-bottom: 0.5rem;">',
         '<div class="flex-between mb-sm">'),
        ('<div class="flex-center" style="gap: 0.5rem;">',
         '<div class="flex-center gap-sm">'),
        ('<p class="text-muted" style="font-size: 0.875rem;">Checking subsystem status...</p>',
         '<p class="text-muted text-sm">Checking subsystem status...</p>'),
    ])

    # ================================================================
    # agents.html (10 -> 4)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "agents.html", [
        # Fix duplicate class first
        ('<div class="form-row gap-sm mt-sm" class="flex-wrap gap-sm">',
         '<div class="form-row gap-sm mt-sm flex-wrap">'),
        ('<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                               class="badge badge-teal" style="cursor:pointer; text-decoration:none; font-size:0.75rem;"\n                               x-text="domain"></a>',
         '<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                               class="badge badge-teal cursor-pointer no-underline text-xs"\n                               x-text="domain"></a>'),
        ('<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                               class="badge" style="cursor:pointer; text-decoration:none; font-size:0.75rem; background:rgba(99,102,241,0.15); color:#818cf8;"\n                               x-text="\'sensor:\' + dc"></a>',
         '<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                               class="badge cursor-pointer no-underline text-xs" style="background:rgba(99,102,241,0.15); color:#818cf8;"\n                               x-text="\'sensor:\' + dc"></a>'),
        ('<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                           class="badge badge-teal" style="cursor:pointer; text-decoration:none; font-size:0.75rem;">Entity rules</a>',
         '<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                           class="badge badge-teal cursor-pointer no-underline text-xs">Entity rules</a>'),
        ('<span class="badge badge-muted" style="font-size:0.75rem; opacity:0.6;">No entity access</span>',
         '<span class="badge badge-muted text-xs opacity-60">No entity access</span>'),
        ('<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                           class="badge badge-muted" style="cursor:pointer; text-decoration:none; font-size:0.75rem;">All entities</a>',
         '<a :href="window.dashUrl(\'/dashboard/entity-index\') + \'?agent=\' + encodeURIComponent(agent.agent_id)"\n                           class="badge badge-muted cursor-pointer no-underline text-xs">All entities</a>'),
        ('<span class="badge badge-purple" style="font-size:0.75rem;"\n                                  x-text="\'MCP: \' + serverGroup[0] + \' (\' + serverGroup[1] + \')\'"></span>',
         '<span class="badge badge-purple text-xs"\n                                  x-text="\'MCP: \' + serverGroup[0] + \' (\' + serverGroup[1] + \')\'"></span>'),
        ('<span class="mono" x-text="agent.temperature" style="font-size: 0.75rem;"></span>',
         '<span class="mono text-xs" x-text="agent.temperature"></span>'),
        ('<label class="badge" style="cursor:pointer; font-size:0.75rem; user-select:none;"\n                                           :class="isServerAssigned(agent.agent_id, server.name) ? \'badge-purple\' : \'badge-muted\'">',
         '<label class="badge cursor-pointer text-xs" style="user-select:none;"\n                                           :class="isServerAssigned(agent.agent_id, server.name) ? \'badge-purple\' : \'badge-muted\'">'),
    ])

    # ================================================================
    # personality.html (11 -> 2)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "personality.html", [
        ('<input\n                type="range"\n                class="form-input"\n                min="0"\n                max="1"\n                step="0.1"\n                x-model.number="config.mediation_temperature"\n                style="width: 100%;"\n            />',
         '<input\n                type="range"\n                class="form-input w-full"\n                min="0"\n                max="1"\n                step="0.1"\n                x-model.number="config.mediation_temperature"\n            />'),
        ('<span x-show="saved" class="text-sage" style="font-size: 0.875rem;">Saved</span>',
         '<span x-show="saved" class="text-sage text-sm">Saved</span>'),
        ('<span x-show="error" class="text-coral" style="font-size: 0.875rem;" x-text="error"></span>',
         '<span x-show="error" class="text-coral text-sm" x-text="error"></span>'),
        ('<div class="card card-narrow" style="margin-top: 1.5rem;">',
         '<div class="card card-narrow mt-lg">'),
        ('<p class="text-muted mb-md" style="font-size: 0.875rem;">',
         '<p class="text-muted mb-md text-sm">'),
        ('<label class="form-label" style="display: flex; align-items: center; gap: 0.5rem;">',
         '<label class="form-label flex-center">'),
        ('<input type="checkbox" x-model="config.filler_enabled" style="width: auto;">',
         '<input type="checkbox" x-model="config.filler_enabled" class="w-auto">'),
        ('<input type="range" class="form-input" min="500" max="5000" step="100"\n                   x-model.number="config.filler_threshold_ms" style="width: 100%;">',
         '<input type="range" class="form-input w-full" min="500" max="5000" step="100"\n                   x-model.number="config.filler_threshold_ms">'),
        ('<div class="card card-narrow" style="margin-top: 1.5rem;">',
         '<div class="card card-narrow mt-lg">'),
        ('<p class="text-muted" style="font-size: 0.875rem;">',
         '<p class="text-muted text-sm">'),
    ])

    # ================================================================
    # analytics.html (14 -> 5)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "analytics.html", [
        ('<select class="form-input" x-model.number="hours" @change="loadAll()" style="width: 140px;">',
         '<select class="form-input w-140" x-model.number="hours" @change="loadAll()">'),
        ('<div class="stat-card-icon" style="background: rgba(94,234,212,0.10);">',
         '<div class="stat-card-icon bg-teal-10">'),
        ('<div class="stat-card-icon" style="background: rgba(251,191,36,0.10);">',
         '<div class="stat-card-icon bg-amber-10">'),
        ('<div class="stat-card-icon" style="background: rgba(134,239,172,0.10);">',
         '<div class="stat-card-icon bg-green-10">'),
        ('<div class="stat-card-icon" style="background: rgba(96,165,250,0.10);">',
         '<div class="stat-card-icon bg-blue-10">'),
        ('<div class="text-muted" style="font-size: 0.75rem;">Total</div>',
         '<div class="text-muted text-xs">Total</div>'),
        ('<div style="font-size: 1.25rem; font-weight: 600;" x-text="rewriteData.total ?? 0"></div>',
         '<div class="text-lg" x-text="rewriteData.total ?? 0"></div>'),
        ('<div class="text-muted" style="font-size: 0.75rem;">Avg Latency</div>',
         '<div class="text-muted text-xs">Avg Latency</div>'),
        ('<div style="font-size: 1.25rem; font-weight: 600;" x-text="(rewriteData.avg_latency_ms ?? 0) + \'ms\'"></div>',
         '<div class="text-lg" x-text="(rewriteData.avg_latency_ms ?? 0) + \'ms\'"></div>'),
        ('<div class="text-muted" style="font-size: 0.75rem;">Successes</div>',
         '<div class="text-muted text-xs">Successes</div>'),
        ('<div class="text-sage" style="font-size: 1.25rem; font-weight: 600;" x-text="rewriteData.successes ?? 0"></div>',
         '<div class="text-sage text-lg" x-text="rewriteData.successes ?? 0"></div>'),
        ('<div class="text-muted" style="font-size: 0.75rem;">Failures</div>',
         '<div class="text-muted text-xs">Failures</div>'),
        ('<div class="text-coral" style="font-size: 1.25rem; font-weight: 600;" x-text="rewriteData.failures ?? 0"></div>',
         '<div class="text-coral text-lg" x-text="rewriteData.failures ?? 0"></div>'),
    ])

    # ================================================================
    # timers.html (15 -> 7)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "timers.html", [
        ('<label class="card flex-between" style="padding:0.85rem;">',
         '<label class="card flex-between p-form">'),
        ('<label class="card flex-between" style="padding:0.85rem;">',
         '<label class="card flex-between p-form">'),
        ('<label class="card flex-between" style="padding:0.85rem;">',
         '<label class="card flex-between p-form">'),
        ('<label class="card flex-between" style="padding:0.85rem;">',
         '<label class="card flex-between p-form">'),
        ('<label class="card flex-between" style="padding:0.85rem;">',
         '<label class="card flex-between p-form">'),
        ('<label class="card flex-between" style="padding:0.85rem;">',
         '<label class="card flex-between p-form">'),
        ('<p class="text-muted" style="padding: 1rem 0;">Loading timer data...</p>',
         '<p class="text-muted pt-md pb-md">Loading timer data...</p>'),
        ('<label class="card flex-between" style="padding:0.85rem;margin-bottom:1rem;">',
         '<label class="card flex-between p-form mb-md">'),
        ('<div class="card" style="padding:0.85rem;margin-bottom:1rem;">',
         '<div class="card p-form mb-md">'),
    ])

    # ================================================================
    # calendar.html (20 -> 12)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "calendar.html", [
        ('<div class="form-group" style="flex: 1; min-width: 180px;">',
         '<div class="form-group flex-1 min-w-180">'),
        ('<div class="form-group" style="flex: 1; min-width: 220px;">',
         '<div class="form-group flex-1 min-w-220">'),
        ('<div class="form-group" style="flex: 1; min-width: 220px;">',
         '<div class="form-group flex-1 min-w-220">'),
        ('<div class="form-group" style="flex: 1; min-width: 180px;">',
         '<div class="form-group flex-1 min-w-180">'),
        ('<div class="form-group" style="flex: 1; min-width: 180px;">',
         '<div class="form-group flex-1 min-w-180">'),
        ('<div class="form-group" style="flex: 1; min-width: 180px;">',
         '<div class="form-group flex-1 min-w-180">'),
        ('<div class="form-group" style="flex: 0 0 120px; align-self: flex-end;">',
         '<div class="form-group self-end" style="flex: 0 0 120px;">'),
        ('<label class="toggle" style="margin-top: 0.5rem;">',
         '<label class="toggle mt-sm">'),
        ('<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem;">',
         '<div class="flex-between mb-md">'),
    ])

    # ================================================================
    # settings.html (27 -> 16)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "settings.html", [
        # Fix malformed HTML first
        ('<span x-show="!providers[p.id] || !providers[p.id].configured" class="badge" style="font-size:0.7rem; class="badge badge-danger"">No key</span>',
         '<span x-show="!providers[p.id] || !providers[p.id].configured" class="badge badge-danger text-2xs">No key</span>'),
        ('<span x-show="!haConn.token_configured" class="badge" style="font-size:0.7rem; class="badge badge-danger"">No token</span>',
         '<span x-show="!haConn.token_configured" class="badge badge-danger text-2xs">No token</span>'),
        ('<span class="text-mono" class="text-sm">Authorization: Bearer &lt;this key&gt;</span>',
         '<span class="text-mono text-sm">Authorization: Bearer &lt;this key&gt;</span>'),
        ('<div class="form-group" class="mb-sm">',
         '<div class="form-group mb-sm">'),
        ('<div class="mt-sm" class="card">',
         '<div class="mt-sm card">'),
        # Replacements
        ('<span x-show="providers[p.id] && providers[p.id].configured" class="badge badge-teal" style="font-size:0.7rem;"',
         '<span x-show="providers[p.id] && providers[p.id].configured" class="badge badge-teal text-2xs"'),
        ('<input :type="p.type" class="form-input" x-model="providerKeys[p.id]" :placeholder="p.placeholder" style="margin:0;">',
         '<input :type="p.type" class="form-input" x-model="providerKeys[p.id]" :placeholder="p.placeholder">'),
        ('<h4 class="section-header mt-md mb-sm" style="font-size: 0.875rem;">Signal Weights</h4>',
         '<h4 class="section-header mt-md mb-sm text-sm">Signal Weights</h4>'),
        ('<h4 class="section-header mb-sm" style="font-size: 0.875rem;">Home Assistant</h4>',
         '<h4 class="section-header mb-sm text-sm">Home Assistant</h4>'),
        ('<input type="password" class="form-input" style="flex:1; min-width:200px;"',
         '<input type="password" class="form-input flex-1 min-w-200"'),
        ('<span x-show="haConn.token_configured" class="badge badge-teal" style="font-size:0.7rem;"',
         '<span x-show="haConn.token_configured" class="badge badge-teal text-2xs"'),
        ('<h4 class="section-header mt-md mb-sm" style="font-size: 0.875rem;">Home Assistant integration (HA to Agent Hub)</h4>',
         '<h4 class="section-header mt-md mb-sm text-sm">Home Assistant integration (HA to Agent Hub)</h4>'),
        ('<span class="text-mono" style="font-size: 0.75rem;">/api/conversation</span>',
         '<span class="text-mono text-xs">/api/conversation</span>'),
        ('<div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap; margin-bottom:0.5rem;">',
         '<div class="flex-center flex-wrap mb-sm">'),
        ('<span x-show="containerApiKey.configured" class="badge badge-teal" style="font-size:0.7rem;"',
         '<span x-show="containerApiKey.configured" class="badge badge-teal text-2xs"'),
        ('<span x-show="!containerApiKey.configured" class="badge" style="font-size:0.7rem; background:var(--danger, #ef4444); color:#fff;">No key configured</span>',
         '<span x-show="!containerApiKey.configured" class="badge text-2xs" style="background:var(--danger, #ef4444); color:#fff;">No key configured</span>'),
        ('<input type="password" class="form-input" style="flex:1; min-width:200px;"',
         '<input type="password" class="form-input flex-1 min-w-200"'),
        ('<div style="display:flex; gap:0.35rem; flex-wrap:wrap; align-items:center;">',
         '<div class="flex-wrap" style="display:flex; gap:0.35rem; align-items:center;">'),
        ('<div style="display:flex; gap:0.35rem; flex-wrap:wrap;">',
         '<div style="display:flex; gap:0.35rem;" class="flex-wrap">'),
        ('<h4 class="section-header mt-md mb-sm" style="font-size: 0.875rem;">Voice / streaming to HA</h4>',
         '<h4 class="section-header mt-md mb-sm text-sm">Voice / streaming to HA</h4>'),
    ])

    # ================================================================
    # cache.html (35 -> 6)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "cache.html", [
        ('<div class="stat-card" style="grid-column: span 3;">',
         '<div class="stat-card col-span-3">'),
        ('<div class="stat-card-icon" style="background: rgba(96,165,250,0.10);">',
         '<div class="stat-card-icon bg-blue-10">'),
        ('<div style="border-right: 1px solid rgba(255,255,255,0.06); padding-right: 1rem;">',
         '<div class="border-right-subtle pr-md">'),
        ('<div class="flex-center" style="gap: 0.5rem; justify-content: flex-start; margin-bottom: 0.5rem;">',
         '<div class="flex-center justify-start mb-sm">'),
        ('<span class="text-teal" style="font-size: 0.875rem; font-weight: 600;">Export</span>',
         '<span class="text-teal text-sm" style="font-weight: 600;">Export</span>'),
        ('<div class="flex-center" style="gap: 0.5rem; justify-content: flex-start; margin-bottom: 0.5rem;">',
         '<div class="flex-center justify-start mb-sm">'),
        ('<span class="text-sage" style="font-size: 0.875rem; font-weight: 600;">Import</span>',
         '<span class="text-sage text-sm" style="font-weight: 600;">Import</span>'),
        ('<div class="btn-group" style="align-items: center;">',
         '<div class="btn-group">'),
        ('<div x-show="backupMessage" class="text-sage mt-sm" style="font-size: 0.75rem;" x-text="backupMessage"></div>',
         '<div x-show="backupMessage" class="text-sage mt-sm text-xs" x-text="backupMessage"></div>'),
        ('<div class="stat-card-icon" style="background: rgba(94,234,212,0.10);">',
         '<div class="stat-card-icon bg-teal-10">'),
        ('<input type="text" class="form-input" x-model="search" placeholder="Search entries..." style="width: 200px;">',
         '<input type="text" class="form-input w-200" x-model="search" placeholder="Search entries...">'),
        ('<button class="btn btn-primary btn-sm" @click="page = 1; loadEntries()" style="align-self: flex-end;">Search</button>',
         '<button class="btn btn-primary btn-sm self-end" @click="page = 1; loadEntries()">Search</button>'),
        ('<span class="text-muted" style="font-size: 0.875rem;" x-text="\'Showing \' + entries.length + \' of \' + entryTotal"></span>',
         '<span class="text-muted text-sm" x-text="\'Showing \' + entries.length + \' of \' + entryTotal"></span>'),
        ('<div class="flex-center" style="gap: 0.5rem;">',
         '<div class="flex-center">'),
        ('<span class="text-mono" style="font-size: 0.875rem;" x-text="page + \' / \' + entryPages"></span>',
         '<span class="text-mono text-sm" x-text="page + \' / \' + entryPages"></span>'),
        ('<td class="truncate" style="max-width: 400px;" x-text="e.document"></td>',
         '<td class="truncate max-w-400" x-text="e.document"></td>'),
        ('<td class="text-mono" style="font-size: 0.75rem;" x-text="e.last_accessed || \'-\'"></td>',
         '<td class="text-mono text-xs" x-text="e.last_accessed || \'-\'"></td>'),
        ('<div x-show="flushMessage && activeTab === \'routing\'" class="text-sage mt-sm" style="font-size: 0.75rem;" x-text="flushMessage"></div>',
         '<div x-show="flushMessage && activeTab === \'routing\'" class="text-sage mt-sm text-xs" x-text="flushMessage"></div>'),
        ('<div x-show="flushMessage && activeTab === \'action\'" class="text-sage mt-sm" style="font-size: 0.75rem;" x-text="flushMessage"></div>',
         '<div x-show="flushMessage && activeTab === \'action\'" class="text-sage mt-sm text-xs" x-text="flushMessage"></div>'),
        ('<input type="text" class="form-input" x-model="search" placeholder="Search entries..." style="width: 200px;">',
         '<input type="text" class="form-input w-200" x-model="search" placeholder="Search entries...">'),
        ('<button class="btn btn-primary btn-sm" @click="page = 1; loadEntries()" style="align-self: flex-end;">Search</button>',
         '<button class="btn btn-primary btn-sm self-end" @click="page = 1; loadEntries()">Search</button>'),
        ('<span class="text-muted" style="font-size: 0.875rem;" x-text="\'Showing \' + entries.length + \' of \' + entryTotal"></span>',
         '<span class="text-muted text-sm" x-text="\'Showing \' + entries.length + \' of \' + entryTotal"></span>'),
        ('<div class="flex-center" style="gap: 0.5rem;">',
         '<div class="flex-center">'),
        ('<span class="text-mono" style="font-size: 0.875rem;" x-text="page + \' / \' + entryPages"></span>',
         '<span class="text-mono text-sm" x-text="page + \' / \' + entryPages"></span>'),
        ('<td class="truncate" style="max-width: 400px;" x-text="e.document"></td>',
         '<td class="truncate max-w-400" x-text="e.document"></td>'),
        ('<td class="text-mono" style="font-size: 0.75rem;" x-text="e.last_accessed || \'-\'"></td>',
         '<td class="text-mono text-xs" x-text="e.last_accessed || \'-\'"></td>'),
    ])

    # ================================================================
    # entity_index.html (37 -> 20)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "entity_index.html", [
        # Fix duplicate classes
        ('<div class="stat-card-value" class="text-sm" x-text="stats.last_refresh || \'never\'"></div>',
         '<div class="stat-card-value text-sm" x-text="stats.last_refresh || \'never\'"></div>'),
        ('<div class="stat-card-value" class="text-sm"\n                     x-text="\'+\' + (stats.sync?.added || 0) + \' ~\' + (stats.sync?.updated || 0) + \' -\' + (stats.sync?.removed || 0) + \' =\' + (stats.sync?.unchanged || 0)">',
         '<div class="stat-card-value text-sm"\n                     x-text="\'+\' + (stats.sync?.added || 0) + \' ~\' + (stats.sync?.updated || 0) + \' -\' + (stats.sync?.removed || 0) + \' =\' + (stats.sync?.unchanged || 0)">'),
        ('<div class="stat-card-value" class="text-sm" x-text="stats.embedding?.model || \'-\'"></div>',
         '<div class="stat-card-value text-sm" x-text="stats.embedding?.model || \'-\'"></div>'),
        ('<div class="stat-card-value" style="font-size: 0.95rem;" x-text="previewResult.deterministic.friendly_name || \'-\'"></div>',
         '<div class="stat-card-value text-95" x-text="previewResult.deterministic.friendly_name || \'-\'"></div>'),
        ('<div class="flex-center gap-sm mt-sm" style="flex-wrap: wrap;">',
         '<div class="flex-center gap-sm mt-sm flex-wrap">'),
        ('<span class="badge" class="text-sm" x-text="k + \': \' + Number(v).toFixed(2)"></span>',
         '<span class="badge text-sm" x-text="k + \': \' + Number(v).toFixed(2)"></span>'),
        ('<div class="mt-sm" style="display: flex; flex-wrap: wrap; gap: 0.25rem;">',
         '<div class="mt-sm flex-wrap" style="display: flex; gap: 0.25rem;">'),
        ('<p class="text-dim mt-sm" style="font-size: 0.8rem;">',
         '<p class="text-dim mt-sm">'),
        ('<th style="width: 120px;"></th>',
         '<th class="w-120"></th>'),
        ('<td style="white-space: nowrap;">',
         '<td class="text-nowrap">'),
        ('<span class="text-dim" style="font-size: 0.75rem; margin-left: 0.375rem;" x-text="item.entity_count + \' entities\'"></span>',
         '<span class="text-dim text-xs" style="margin-left: 0.375rem;" x-text="item.entity_count + \' entities\'"></span>'),
        ('<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 0.375rem;">',
         '<div class="flex-wrap" style="display: flex; align-items: center; gap: 0.375rem;">'),
        ('<span class="badge badge-blue" class="flex-center gap-sm">',
         '<span class="badge badge-blue flex-center gap-sm">'),
        ('<select class="form-input" style="width: auto; min-width: 140px; max-width: 200px; padding: 0.2rem 0.5rem; font-size: 0.75rem; border-radius: 9999px; display: inline-block;"',
         '<select class="form-input w-auto text-xs" style="min-width: 140px; max-width: 200px; padding: 0.2rem 0.5rem; border-radius: 9999px; display: inline-block;"'),
        ('<td style="text-align: right;">',
         '<td class="text-right">'),
        ('<th style="width: 120px;"></th>',
         '<th class="w-120"></th>'),
        ('<span class="text-dim" style="font-size: 0.75rem; margin-left: 0.375rem;" x-text="item.entity_count + \' entities\'"></span>',
         '<span class="text-dim text-xs" style="margin-left: 0.375rem;" x-text="item.entity_count + \' entities\'"></span>'),
        ('<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 0.375rem;">',
         '<div class="flex-wrap" style="display: flex; align-items: center; gap: 0.375rem;">'),
        ('<span class="badge badge-blue" style="display: inline-flex; align-items: center; gap: 0.25rem;">',
         '<span class="badge badge-blue" style="display: inline-flex; align-items: center; gap: 0.25rem;">'),
        ('<select class="form-input" style="width: auto; min-width: 140px; max-width: 200px; padding: 0.2rem 0.5rem; font-size: 0.75rem; border-radius: 9999px; display: inline-block;"',
         '<select class="form-input w-auto text-xs" style="min-width: 140px; max-width: 200px; padding: 0.2rem 0.5rem; border-radius: 9999px; display: inline-block;"'),
        ('<td style="text-align: right;">',
         '<td class="text-right">'),
    ])

    # ================================================================
    # traces.html (70 -> 25)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "traces.html", [
        # Fix duplicate classes
        ('<div class="card mb-md" class="p-md">',
         '<div class="card mb-md p-md">'),
        ('<div class="form-row" class="items-end">',
         '<div class="form-row items-end">'),
        ('<div class="form-group mb-0" style="flex:1;min-width:200px;">',
         '<div class="form-group mb-0 flex-1 min-w-200">'),
        ('<select class="form-input" style="width:auto;min-width:150px;"',
         '<select class="form-input w-auto" style="min-width:150px;"'),
        ('<select class="form-input" style="width:auto;min-width:120px;"',
         '<select class="form-input w-auto" style="min-width:120px;"'),
        ('<input type="date" class="form-input" style="width:auto;"',
         '<input type="date" class="form-input w-auto"'),
        ('<input type="date" class="form-input" style="width: auto;"',
         '<input type="date" class="form-input w-auto"'),
        ('<button class="btn btn-sm btn-secondary" class="self-end" @click="exportCSV()">Export CSV</button>',
         '<button class="btn btn-sm btn-secondary self-end" @click="exportCSV()">Export CSV</button>'),
        ('<span class="text-muted" class="text-sm" x-text="\'Total: \' + total + \' traces\'"></span>',
         '<span class="text-muted text-sm" x-text="\'Total: \' + total + \' traces\'"></span>'),
        ('<div class="flex-center" class="gap-sm">',
         '<div class="flex-center gap-sm">'),
        ('<span class="text-mono" style="font-size: 0.875rem;" x-text="page + \' / \' + pages"></span>',
         '<span class="text-mono text-sm" x-text="page + \' / \' + pages"></span>'),
        ('<td class="text-mono" class="text-sm text-nowrap" x-text="formatTimestamp(t.created_at)"></td>',
         '<td class="text-mono text-sm text-nowrap" x-text="formatTimestamp(t.created_at)"></td>'),
        ('<td class="truncate" style="max-width:300px;" x-text="t.user_input || \'-\'"></td>',
         '<td class="truncate max-w-300" x-text="t.user_input || \'-\'"></td>'),
        ('<span x-show="t.label" class="badge badge-blue" class="text-sm" x-text="t.label"></span>',
         '<span x-show="t.label" class="badge badge-blue text-sm" x-text="t.label"></span>'),
        ('<div class="flex-center" class="gap-lg flex-wrap">',
         '<div class="flex-center gap-lg flex-wrap">'),
        ('<span class="text-muted" class="text-sm">Timestamp</span><br>',
         '<span class="text-muted text-sm">Timestamp</span><br>'),
        ('<span class="text-mono" style="font-size: 0.875rem;" x-text="formatTimestamp(traceDetail.timestamp)"></span>',
         '<span class="text-mono text-sm" x-text="formatTimestamp(traceDetail.timestamp)"></span>'),
        ('<span class="text-muted" style="font-size: 0.75rem;">Session</span><br>',
         '<span class="text-muted text-xs">Session</span><br>'),
        ('<span class="text-mono" style="font-size: 0.875rem;" x-text="traceDetail.conversation_id || \'-\'"></span>',
         '<span class="text-mono text-sm" x-text="traceDetail.conversation_id || \'-\'"></span>'),
        ('<span class="text-muted" style="font-size: 0.75rem;">Duration</span><br>',
         '<span class="text-muted text-xs">Duration</span><br>'),
        ('<span class="text-mono" style="font-size: 0.875rem;" x-text="traceDetail.duration_ms ? Math.round(traceDetail.duration_ms) + \'ms\' : \'-\'"></span>',
         '<span class="text-mono text-sm" x-text="traceDetail.duration_ms ? Math.round(traceDetail.duration_ms) + \'ms\' : \'-\'"></span>'),
        ('<span class="text-muted" style="font-size: 0.75rem;">Source</span><br>',
         '<span class="text-muted text-xs">Source</span><br>'),
        ('<span class="badge badge-blue" class="text-sm" x-text="traceDetail.source || \'-\'"></span>',
         '<span class="badge badge-blue text-sm" x-text="traceDetail.source || \'-\'"></span>'),
        ('<span class="text-muted" style="font-size: 0.75rem;">Satellite</span><br>',
         '<span class="text-muted text-xs">Satellite</span><br>'),
        ('<span class="text-mono" style="font-size: 0.875rem;"\n                                      :title="traceDetail.device_id || \'\'"\n                                      x-text="traceDetail.device_name || traceDetail.device_id || \'-\'"></span>',
         '<span class="text-mono text-sm"\n                                      :title="traceDetail.device_id || \'\'"\n                                      x-text="traceDetail.device_name || traceDetail.device_id || \'-\'"></span>'),
        ('<span class="text-muted" style="font-size: 0.75rem;">Area</span><br>',
         '<span class="text-muted text-xs">Area</span><br>'),
        ('<span class="text-mono" style="font-size: 0.875rem;"\n                                      :title="traceDetail.area_id || \'\'"\n                                      x-text="traceDetail.area_name || traceDetail.area_id || \'-\'"></span>',
         '<span class="text-mono text-sm"\n                                      :title="traceDetail.area_id || \'\'"\n                                      x-text="traceDetail.area_name || traceDetail.area_id || \'-\'"></span>'),
        ('<span class="text-muted" style="font-size: 0.75rem;">Label</span><br>',
         '<span class="text-muted text-xs">Label</span><br>'),
        ('<span x-show="traceDetail.label" class="badge badge-blue" style="font-size: 0.7rem; cursor: pointer;"\n                                              x-text="traceDetail.label" @click="editingLabel = true; newLabel = traceDetail.label || \'\'"></span>',
         '<span x-show="traceDetail.label" class="badge badge-blue text-2xs cursor-pointer"\n                                              x-text="traceDetail.label" @click="editingLabel = true; newLabel = traceDetail.label || \'\'"></span>'),
        ('<button x-show="!traceDetail.label" class="btn btn-sm btn-secondary" style="font-size: 0.7rem; padding: 0.15rem 0.5rem;"\n                                                @click="editingLabel = true; newLabel = \'\'">+ Label</button>',
         '<button x-show="!traceDetail.label" class="btn btn-sm btn-secondary text-2xs" style="padding: 0.15rem 0.5rem;"\n                                                @click="editingLabel = true; newLabel = \'\'">+ Label</button>'),
        ('<input type="text" class="form-input" style="width: 100px; font-size: 0.75rem; padding: 0.25rem 0.5rem;"\n                                               x-model="newLabel" @keyup.enter="saveLabel()" @keyup.escape="editingLabel = false">',
         '<input type="text" class="form-input text-xs" style="width: 100px; padding: 0.25rem 0.5rem;"\n                                               x-model="newLabel" @keyup.enter="saveLabel()" @keyup.escape="editingLabel = false">'),
        ('<div class="card mb-md" style="background:var(--bg-obsidian);">',
         '<div class="card mb-md bg-obsidian">'),
        ('<h4 class="card-title mb-sm" style="font-size: 0.875rem;">User Input</h4>',
         '<h4 class="card-title mb-sm text-sm">User Input</h4>'),
        ('<div class="card mb-md" style="background: var(--bg-obsidian);">',
         '<div class="card mb-md bg-obsidian">'),
        ('<h4 class="card-title mb-sm" style="font-size: 0.875rem;">Final Response</h4>',
         '<h4 class="card-title mb-sm text-sm">Final Response</h4>'),
        ('<pre style="white-space: pre-wrap; word-break: break-word; font-size: 0.875rem; margin: 0; color: var(--color-cloud);" x-text="traceDetail.final_response || \'-\'"></pre>',
         '<pre class="text-sm" style="white-space: pre-wrap; word-break: break-word; margin: 0; color: var(--color-cloud);" x-text="traceDetail.final_response || \'-\'"></pre>'),
        ('<div class="card mb-md" style="background: var(--bg-obsidian);">',
         '<div class="card mb-md bg-obsidian">'),
        ('<h4 class="card-title mb-sm" style="font-size: 0.875rem;">Routing Decision</h4>',
         '<h4 class="card-title mb-sm text-sm">Routing Decision</h4>'),
        ('<span style="display: flex; gap: 0.3rem; flex-wrap: wrap;">',
         '<span class="flex-wrap" style="display: flex; gap: 0.3rem;">'),
        ('<div class="card mb-md" style="background: var(--bg-obsidian);">',
         '<div class="card mb-md bg-obsidian">'),
        ('<h4 class="card-title mb-sm" style="font-size: 0.875rem;">Agent Instructions</h4>',
         '<h4 class="card-title mb-sm text-sm">Agent Instructions</h4>'),
        ('<span style="font-size: 0.875rem; color: var(--color-cloud);" x-text="task"></span>',
         '<span class="text-sm" style="color: var(--color-cloud);" x-text="task"></span>'),
        ('<div class="card mb-md" style="background: var(--bg-obsidian);">',
         '<div class="card mb-md bg-obsidian">'),
        ('<h4 class="card-title mb-sm" style="font-size: 0.875rem;">Agent Communication</h4>',
         '<h4 class="card-title mb-sm text-sm">Agent Communication</h4>'),
        ('<span style="color: var(--text-silver); font-size: 0.75rem;">--&gt;</span>',
         '<span class="text-xs" style="color: var(--text-silver);">--&gt;</span>'),
        ('<span class="text-muted" style="font-size: 0.7rem;">Task:</span>',
         '<span class="text-muted text-2xs">Task:</span>'),
        ('<span style="font-size: 0.75rem; color: var(--teal-glow); font-weight: 600;">Memory</span>',
         '<span class="text-xs" style="color: var(--teal-glow); font-weight: 600;">Memory</span>'),
        ('<div style="margin-bottom: 0.3rem; padding: 0.3rem 0.5rem; border-radius: 4px; font-size: 0.75rem;"',
         '<div class="text-xs" style="margin-bottom: 0.3rem; padding: 0.3rem 0.5rem; border-radius: 4px;"'),
        ('<pre style="white-space: pre-wrap; word-break: break-word; font-size: 0.75rem; margin: 0.15rem 0 0 0; color: var(--color-cloud);"\n                                                             x-text="turn.content"></pre>',
         '<pre class="text-xs" style="white-space: pre-wrap; word-break: break-word; margin: 0.15rem 0 0 0; color: var(--color-cloud);"\n                                                             x-text="turn.content"></pre>'),
        ('<span class="text-muted" style="font-size: 0.7rem;">Response:</span>',
         '<span class="text-muted text-2xs">Response:</span>'),
        ('<div class="card mb-md" style="background: var(--bg-obsidian);">',
         '<div class="card mb-md bg-obsidian">'),
        ('<h4 class="card-title mb-sm" style="font-size: 0.875rem;">Span Timeline</h4>',
         '<h4 class="card-title mb-sm text-sm">Span Timeline</h4>'),
        ('<td class="text-mono" style="font-size: 0.75rem;" x-text="exec.span_name"></td>',
         '<td class="text-mono text-xs" x-text="exec.span_name"></td>'),
        ('<td class="truncate" style="max-width: 300px; font-size: 0.75rem;" x-text="exec.response || \'-\'"></td>',
         '<td class="truncate max-w-300 text-xs" x-text="exec.response || \'-\'"></td>'),
    ])

    # ================================================================
    # _trace_gantt.html (2 -> 1: keep min-height for chart)
    # ================================================================
    replace_in_file(TEMPLATES_DIR / "partials/_trace_gantt.html", [
        ('<div id="gantt-container" style="min-height: 200px; background: var(--bg-obsidian); border-radius: 8px; padding: 0.5rem;"></div>',
         '<div id="gantt-container" class="bg-obsidian" style="min-height: 200px; border-radius: 8px; padding: 0.5rem;"></div>'),
    ])

    # ================================================================
    # Report results
    # ================================================================
    files = sorted(TEMPLATES_DIR.glob("*.html")) + sorted((TEMPLATES_DIR / "partials").glob("*.html"))
    files = [f for f in files if f.name not in ("base.html", "dashboard_base.html", "login.html")]

    total_remaining = 0
    print("\n=== Results ===")
    for f in sorted(files, key=lambda x: -count_styles(x)):
        count = count_styles(f)
        total_remaining += count
        if count > 0:
            print(f"  {f.name}: {count} remaining")
    print(f"\nTotal remaining inline styles: {total_remaining}")


if __name__ == "__main__":
    main()
