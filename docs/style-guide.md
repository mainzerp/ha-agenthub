# UI Style Guide

This document describes the visual conventions for the HA-AgentHub admin dashboard and setup wizard.

## Scope and Intent

The admin UI is dark-only, server-rendered with Jinja2, and styled with modular plain CSS files under `container/app/dashboard/static/css/`. No CSS bundler, npm, or Tailwind is used.

### CSS file layout

| File | Contents |
|------|----------|
| `tokens.css` | Design tokens (colors, spacing, radii, typography, motion, elevation, glass) |
| `base.css` | Global resets, scrollbar rules, `[x-cloak]` |
| `layout.css` | Grid system, sidebar, topbar, main content area, responsive breakpoints |
| `components.css` | Cards, badges, buttons, forms, tables, toasts, modals, nav groups, command palette |
| `auth.css` | Login and setup wizard specific styles |
| `pages/*.css` | Page-specific overrides (overview, analytics, traces, trace_detail, timers, etc.) |

All templates load the core stack in this order:

```html
<link rel="stylesheet" href="{{ static_url(request, '/dashboard/static/css/tokens.css') }}">
<link rel="stylesheet" href="{{ static_url(request, '/dashboard/static/css/base.css') }}">
<link rel="stylesheet" href="{{ static_url(request, '/dashboard/static/css/layout.css') }}">
<link rel="stylesheet" href="{{ static_url(request, '/dashboard/static/css/components.css') }}">
```

Page-specific CSS is loaded via `{% block page_css %}` after the core stack.

## Asset Vendoring Rule

All third-party assets (htmx, Alpine.js, Chart.js, vis-timeline, fonts) are vendored locally under `container/app/dashboard/static/vendor/`. Templates must not reference external CDNs (`unpkg.com`, `jsdelivr.net`, `fonts.googleapis.com`, `fonts.gstatic.com`).

## Design Tokens

Tokens are defined in `tokens.css`.

### Surfaces

| Token | Value | Usage |
|-------|-------|-------|
| `--bg-abyss` | `#0d0c0a` | Deep surface and input background |
| `--bg-void` | `#12110f` | Page background |
| `--bg-obsidian` | `#151512` | Sidebar and secondary surfaces |
| `--bg-basalt` | `#1a1816` | Card surfaces |
| `--bg-charcoal` | `#2e2b28` | Elevated panels |
| `--bg-stone` | `#2e2b28` | Borders and dividers |

### Text

| Token | Value | Usage |
|-------|-------|-------|
| `--color-ash` | `#4a4642` | Muted borders |
| `--color-dust` | `#6b6660` | Labels and hints |
| `--color-fog` | `#8a8580` | Secondary text |
| `--color-mist` | `#a8a29c` | Tertiary text |
| `--color-cloud` | `#d5d0ca` | Body text on dark panels |
| `--color-light` | `#f5f2ee` | Primary text |
| `--color-bright` | `#f5f2ee` | Headings |

### Accent

The `--teal` token name is retained for compatibility; its current value is warm amber.

| Token | Value | Usage |
|-------|-------|-------|
| `--teal` | `#e2a84b` | Primary accent |
| `--teal-dim` | `#c98f38` | Hover states |
| `--teal-glow` | `#f5c26b` | Active and focus states |
| `--teal-pale` | `#f8dcae` | Highlights and glows |

### Semantic

| Token | Value | Usage |
|-------|-------|-------|
| `--success` / `--sage` | `#7dab8c` | Success states |
| `--warning` / `--soft-amber` | `#e2a84b` | Warnings |
| `--danger` / `--coral` | `#d4756b` | Errors and destructive actions |
| `--danger-strong` / `--ember` | `#b85c52` | Strong error emphasis |
| `--info` / `--blue` | `#8fb4cc` | Information |

### Borders

| Token | Value | Usage |
|-------|-------|-------|
| `--border` | `var(--bg-stone)` | Default borders |
| `--border-subtle` | `rgba(226, 168, 75, 0.10)` | Subtle dividers |
| `--border-strong` | `var(--color-ash)` | Emphasised dividers |

### Inputs

| Token | Value | Usage |
|-------|-------|-------|
| `--input-bg` | `var(--bg-abyss)` | Input backgrounds |

### Spacing

| Token | Value |
|-------|-------|
| `--space-1` | `0.25rem` |
| `--space-2` | `0.5rem` |
| `--space-3` | `0.75rem` |
| `--space-4` | `1rem` |
| `--space-5` | `1.5rem` |
| `--space-6` | `2rem` |

### Radii

| Token | Value |
|-------|-------|
| `--radius-sm` | `0.375rem` |
| `--radius-md` | `0.625rem` |
| `--radius-lg` | `0.75rem` |
| `--radius-pill` | `999px` |

### Typography

| Token | Stack |
|-------|-------|
| `--font-body` | `'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif` |
| `--font-display` | `'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` |
| `--font-mono` | `'JetBrains Mono', "SF Mono", Menlo, Consolas, monospace` |

## Component Classes

### Card

```html
<div class="card">
    <h3 class="card-title">Title</h3>
    <p class="card-subtitle">Subtitle</p>
    <div class="card-body">Content</div>
</div>
```

Modifiers: `.card-narrow` (max-width 600px), `.card-orchestrator` (accented border).

### Stat Card

```html
<div class="stat-card">
    <div class="stat-card-header">
        <svg class="stat-card-icon text-teal">...</svg>
        <span class="stat-card-label">LABEL</span>
    </div>
    <div class="stat-card-value">42</div>
</div>
```

### Stat Grid

```html
<div class="stat-grid">...</div>          <!-- auto-fill, minmax(220px, 1fr) -->
<div class="stat-grid-2cols">...</div>    <!-- 1fr 1fr -->
<div class="stat-grid-3">...</div>        <!-- auto-fill, minmax(280px, 1fr) -->
<div class="stat-grid-6">...</div>        <!-- repeat(3, 1fr) -->
```

### Chart Card

```html
<div class="chart-card">
    <h3 class="chart-card-title">Title</h3>
    <div class="chart-canvas-wrap">
        <canvas id="myChart"></canvas>
    </div>
</div>
```

### Badge

| Class | Usage |
|-------|-------|
| `.badge-green` | Success / enabled |
| `.badge-red` | Error / disabled |
| `.badge-yellow` | Warning / paused |
| `.badge-blue` | Info / primary |
| `.badge-teal` | Accent / internal |
| `.badge-purple` | MCP / custom |
| `.badge-muted` | Neutral / default |
| `.badge-danger` | Strong error (white on red) |
| `.badge-outline` | Transparent with border |
| `.badge-lg` | Larger padding |

Use `.badge-close-btn` for the remove button inside `.badge-blue.badge-close`.

### Alert

```html
<div class="alert alert-success">...</div>
<div class="alert alert-error">...</div>
<div class="alert alert-info">...</div>
<div class="alert alert-warning">...</div>
```

Use `.alpine-warning` for the defensive no-Alpine banner (hidden by default).

### Toast

Toasts are pushed via JavaScript:

```js
window.toast('Settings saved', 'success');
window.toast('Save failed', 'error');
```

Kinds: `success`, `error`, `info`.

### Button

| Class | Usage |
|-------|-------|
| `.btn-primary` | Main action |
| `.btn-secondary` | Secondary action |
| `.btn-danger` | Destructive action |
| `.btn-sm` | Small button |
| `.btn-icon` | Icon-only button |

Group buttons with `.btn-group`.

### Form

```html
<div class="form-group">
    <label class="form-label">Label</label>
    <input type="text" class="form-input">
</div>
```

Variants: `.form-select`, `.form-textarea`, `.form-input-inline` (small inline select), `.form-input-native` (native-styled flex input).
Layout: `.form-grid`, `.form-grid-2`, `.form-row`.

### Table

```html
<div class="table-container">
    <table class="data-table">
        <thead>...</thead>
        <tbody>...</tbody>
    </table>
</div>
```

Use `.table-container-flush` for borderless transparent tables.

Stacked tables: add `.data-table--stack` to turn rows into label/value cards under 768px (header hidden). Every `td` needs `data-label="Column"`; mark the primary cell `td.cell-title` (full-width block, wraps) and the button cell `td.cell-actions` (full-width buttons, no label). Opt-in per table; pages that do not set the class keep the horizontally scrolling table.

### Modal

```html
<div class="modal-overlay" role="dialog" aria-modal="true">
    <div class="card">...</div>
</div>
```

Focus management is handled by `dashModal()`.

### Tab Navigation

```html
<div class="tab-nav" role="tablist">
    <button class="tab-btn active" role="tab" aria-selected="true">Tab 1</button>
    <button class="tab-btn" role="tab" aria-selected="false">Tab 2</button>
</div>
```

### Empty State

```html
<div class="empty-state">
    <p>No data found.</p>
</div>
```

### Skeleton

```html
<div class="skeleton skeleton-text"></div>
<div class="skeleton skeleton-value"></div>
<div class="skeleton skeleton-card"></div>
```

## Utility Classes

One-off spacing, typography, and layout adjustments should use utilities rather than inline styles.

### Typography

| Class | Value |
|-------|-------|
| `.text-3xs` | `0.65rem` |
| `.text-2xs` | `0.7rem` |
| `.text-xs` | `0.75rem` |
| `.text-sm` | `0.875rem` |
| `.text-95` | `0.95rem` |
| `.text-lg` | `1.25rem` / `font-weight: 600` |
| `.font-normal` | `font-weight: normal` |
| `.text-center` | `text-align: center` |
| `.text-nowrap` | `white-space: nowrap` |
| `.text-right` | `text-align: right` |
| `.italic` | `font-style: italic` |
| `.tabular-nums` | `font-variant-numeric: tabular-nums` |
| `.break-all` | `word-break: break-all` |
| `.pre-wrap` | `white-space: pre-wrap` |

### Spacing

| Class | Value |
|-------|-------|
| `.mt-sm` | `margin-top: 0.5rem` |
| `.mt-md` | `margin-top: 1rem` |
| `.mt-lg` | `margin-top: 1.5rem` |
| `.mb-sm` | `margin-bottom: 0.5rem` |
| `.mb-md` | `margin-bottom: 1rem` |
| `.mb-lg` | `margin-bottom: 1.5rem` |
| `.mb-form` | `margin-bottom: 1rem` |
| `.ml-xs` | `margin-left: 0.25rem` |
| `.ml-md` | `margin-left: 1rem` |
| `.mr-xs` | `margin-right: 0.25rem` |
| `.mx-auto` | `margin-left: auto; margin-right: auto` |
| `.m-0` | `margin: 0` |
| `.p-form` | `padding: 0.85rem` |
| `.p-sm` | `padding: 0.5rem` |
| `.p-md` | `padding: 1rem` |
| `.p-0` | `padding: 0` |
| `.pr-md` | `padding-right: 1rem` |
| `.pt-md` | `padding-top: 1rem` |
| `.pb-md` | `padding-bottom: 1rem` |
| `.py-md` | `padding-top: 1rem; padding-bottom: 1rem` |

### Layout

| Class | Value |
|-------|-------|
| `.flex-1` | `flex: 1` |
| `.flex-1-240` | `flex: 1 1 240px` |
| `.flex-none-w-180` | `flex: 0 0 180px` |
| `.flex-wrap` | `flex-wrap: wrap` |
| `.gap-xs` | `gap: 0.25rem` |
| `.gap-sm` | `gap: 0.5rem` |
| `.gap-md` | `gap: 0.75rem` |
| `.justify-start` | `justify-content: flex-start` |
| `.justify-end` | `justify-content: flex-end` |
| `.items-start` | `align-items: flex-start` |
| `.items-end` | `align-items: flex-end` |
| `.self-end` | `align-self: flex-end` |
| `.col-span-3` | `grid-column: span 3` |
| `.col-span-full` | `grid-column: 1 / -1` |

### Width / Height

| Class | Value |
|-------|-------|
| `.w-auto` | `width: auto` |
| `.w-120` | `width: 120px` |
| `.w-140` | `width: 140px` |
| `.w-200` | `width: 200px` |
| `.w-full` | `width: 100%` |
| `.min-w-0` | `min-width: 0` |
| `.min-w-160` | `min-width: 160px` |
| `.min-w-180` | `min-width: 180px` |
| `.min-w-200` | `min-width: 200px` |
| `.min-w-220` | `min-width: 220px` |
| `.max-w-128` | `max-width: 8rem` |
| `.max-w-220` | `max-width: 220px` |
| `.max-w-300` | `max-width: 300px` |
| `.max-w-400` | `max-width: 400px` |
| `.max-w-720` | `max-width: 720px` |
| `.chart-wrap-xs` | `position: relative; height: 160px` |
| `.chart-wrap-sm` | `position: relative; height: 200px` |
| `.chart-wrap-md` | `position: relative; height: 240px` |
| `.chart-wrap-lg` | `position: relative; height: 320px` |
| `.chart-wrap-xl` | `position: relative; height: 400px` |

### Visual

| Class | Value |
|-------|-------|
| `.cursor-pointer` | `cursor: pointer` |
| `.no-underline` | `text-decoration: none` |
| `.opacity-60` | `opacity: 0.6` |
| `.opacity-70` | `opacity: 0.7` |
| `.opacity-100` | `opacity: 1` |
| `.bg-obsidian` | `background: var(--bg-obsidian)` |
| `.bg-teal-10` | `background: rgba(226,168,75,0.10)` |
| `.bg-amber-10` | `background: rgba(240,178,92,0.10)` |
| `.bg-green-10` | `background: rgba(125,171,140,0.10)` |
| `.bg-blue-10` | `background: rgba(143,180,204,0.10)` |
| `.border-right-subtle` | `border-right: 1px solid var(--border-subtle)` |
| `.rounded-md` | `border-radius: 0.5rem` |
| `.rounded-full` | `border-radius: 9999px` |
| `.hidden` | `display: none` |
| `.sr-only` | Visually hidden but accessible |
| `.border-none` | `border: none` |
| `.bg-none` | `background: none` |
| `.leading-none` | `line-height: 1` |
| `.inline-block` | `display: inline-block` |
| `.box-border` | `box-sizing: border-box` |

## Page Chrome Conventions

Every dashboard page should use this structure inside `page_content`:

```html
<div class="page-header">
    <div class="page-header-text">
        <p class="page-subtitle">...</p>
    </div>
    <div class="page-actions">
        <!-- buttons -->
    </div>
</div>
```

## Mobile Conventions

- Breakpoints are 768px (sidebar becomes an overlay with `.sidebar-backdrop`, grids collapse) and 480px (tighter card and table padding, single-column stat grids).
- `layout.css` loads before `components.css`, so a mobile override of an equal-specificity component rule must live in the `Mobile hardening` block at the end of `components.css`; the same rule in `layout.css` is silently ignored.
- `.card` sets `overflow-wrap: anywhere`, which shrinks a table's minimum width to one character. `.data-table` cells therefore reset it to `normal`; wide tables scroll inside `.table-container` or `.table-container-flush` instead of crushing columns. Keep every table inside one of those containers.
- `.btn` and `.badge` never wrap. Rows that hold buttons or badges next to text need `flex-wrap` (or the `.flex-between` / `.form-actions` mobile wrap); a button with long multi-part content uses `.btn-wrap`.
- Fixed-width inputs (`.w-200`) fill the row at 768px and below.

## Sidebar IA

The sidebar is data-driven from `app.dashboard.nav_config.NAV_GROUPS`. Five collapsible groups:

1. **Operate** — Overview, Chat, System Health, Analytics, Traces
2. **Configure** — Agents, Custom Agents, Personality, Entity Index, MCP Servers, Plugins
3. **Domain Data** — Send Devices, Timers, Calendar, Persons, Voice Profiles
4. **Performance** — Cache
5. **System** — Settings

Group expand/collapse state persists to `localStorage` key `dash.sidebar.groups`.

## Shared JavaScript Components

All shared Alpine.js factories live in `components.js` and are registered on `window`:

| Helper | Purpose | Example |
|--------|---------|---------|
| `dashPage(opts)` | Page lifecycle, polling, mount/unmount hooks | `x-data="dashPage({...})"` |
| `dashDataTable(opts)` | Sort, paginate, filter | `x-data="dashDataTable({...})"` |
| `dashSidebarGroups()` | Sidebar group expand/collapse with localStorage | `x-data="dashSidebarGroups()"` |
| `dashCommandPalette()` | Cmd+K/Ctrl+K global palette with keyboard navigation | Mounted once in `dashboard_base.html` |
| `dashLiveStream(url, opts)` | SSE with exponential backoff and polling fallback | `x-init="stream = dashLiveStream('/api/admin/overview/stream', {...})"` |
| `dashModal()` | Modal open/close with initial focus and focus restoration | `x-data="dashModal()"` |
| `dashToasts()` | Alpine toast queue factory | Used by `#toast-root` |
| `window.toast(msg, kind)` | Push a toast notification | `window.toast('Saved', 'success')` |
| `window.dashModelCatalog` | Per-page memoized provider model lists (`fetchModels(provider, {refresh})` never throws; `invalidate`, `fallbackModels`, `placeholder`) plus model `<select>` helpers (`loadList`, `pickerOptions`, `pickerValue`, `statusText`, `findModel`, `CUSTOM_MODEL`) shared by every model dropdown | `await dashModelCatalog.fetchModels('anthropic')` |
| `window.dashboardApi` | Authenticated fetch wrapper | `await dashboardApi.json('/api/admin/settings')` |
| `window.dashUrl()` | `root_path`-aware URL builder | `dashUrl('/dashboard/agents')` |
| `window.chartColors()` | Read named design colors from CSS | `chartColors().teal` |
| `window.chartRgba(token, alpha)` | Convert a named chart color to rgba | `chartRgba('teal', 0.5)` |
| `window.dashFormatRelativeTime(ts)` | Human-readable relative time | `dashFormatRelativeTime(Date.now())` |
| `window.dashFormatBytes(n)` | Human-readable bytes | `dashFormatBytes(1024)` |
| `window.dashTruncate(s, n)` | Truncate with ellipsis | `dashTruncate('hello world', 8)` |

## Activity Orb widget

`<agenthub-activity-orb>` (`static/activity-orb.js`) visualizes the orchestrator (central additive point cloud with a faint nearest-neighbour web) and the seven agents it used most recently (satellites on a tilted ring, each a miniature of the central sphere in its own colour, joined to it by straight lines). It is a self-contained Custom Element: Shadow DOM, plain Canvas2D, no Alpine/HTMX/Chart.js dependency. It targets same-origin dashboard pages (cookie auth, `window.dashUrl` paths); cross-origin embedding (Home Assistant) is tracked on the roadmap. `dashboard_base.html` loads it with `defer`, so any dashboard page can use the tag.

Embed on another dashboard page:

```html
<agenthub-activity-orb heading="Agent Activity"></agenthub-activity-orb>
<!-- previews without a backend: add the `demo` attribute -->
```

| Attribute | Effect |
|-----------|--------|
| `demo` | Simulated events (classify phase, 1-2 parallel dispatches, ~12% errors); no EventSource is opened. Runs a repeating cycle: a calm idle phase with no events at all, then a burst of requests |
| `demo-idle` | Demo only: seconds of calm idle between bursts, randomised by 20% (default `10`); the first idle phase after start-up is 20% longer |
| `demo-burst` | Demo only: requests per burst (default `2`, 1 to 6); a burst lasts about 5 to 8 s |
| `transport` | `external`: the widget opens no EventSource and makes no fetches; the host drives it through the integration API below (default: same-origin SSE) |
| `bare` | No card chrome, border, padding or heading: a dark rounded stage only, filling its container (used by the Lovelace card) |
| `transparent` | Remove the stage fill and the card chrome: the orb floats on whatever is behind it and switches to the light-surface render profile (see Render profiles) when that surface is light |
| `stage-background` | A CSS colour for the stage fill; wins over the dark default and over `transparent` for the stage, and the render profile follows its luminance |
| `heading` | Card title, default `Agent Activity` |
| `compact` | No heading and tighter padding; the stage keeps its 16:10 shape (1:1 when the widget is under 480px wide) |
| `fixed-height` | Fix the stage height to the CSS custom property `--orb-stage-height` (set by the card for an explicit `height`); the stage then fills the width |
| `span-timeout` | Diagnostics: ms before an unclosed span is released (default 120000) |

Integration API (methods on the element; nothing else is needed to drive it from another transport):

| Member | Purpose |
|--------|---------|
| `el.pushEvent(evt)` | Feed one activity payload (the SSE `activity` data shape) |
| `el.applySnapshot({agents: [{agent_id, name, last_used}]})` | Seed or refresh the 7 slots like the `/api/admin/activity/recent` response (most recent first). Agents that are already shown keep their slot, so repeated calls never cause visible jumps |
| `el.setConnectionState('live' \| 'reconnecting' \| 'offline')` | Drive the corner indicator (its colour change is eased) |
| `el.refreshSurface()` | Re-detect the surface behind a transparent orb (call after a theme or background change; the card does it on theme changes) |
| `el.diagnostics` | Read-only state snapshot for tests |
| `agenthub-orb-phase` event | Demo mode only: bubbling, composed `CustomEvent`, `detail.phase` is `idle` or `activity`, fired on phase changes |

Event contract: shared singleton `EventSource` to `GET /api/admin/activity/stream` (built with `window.dashUrl`), SSE event name `activity`, JSON `{type: "span_start"|"span_end", span_id, trace_id, span_name, agent_id|null, ts, status: "ok"|"error"|null, duration_ms|null, source|null}`. Producer side is documented in `docs/api-reference.md`.

- `dispatch*` spans with an `agent_id` drive satellites: an expanding shockwave ring around the sphere and a glowing pulse with a short trail travelling the straight link outward, a brighter glowing link plus lit node and spinner while open, a return pulse and a duration chip on completion, danger color on `status: "error"`. Null or pseudo ids (`orchestrator`, `noise`, `cancel-interaction`) and unknown span names are ignored.
- `classify`/`classify.*`, `cache_lookup`, `rewrite` and `return` spans energize the central sphere while any is open: larger surface displacement, faster rotation, brighter core and web, spinner ring.
- Satellites are miniature spheres: shell-weighted points with a nearest-neighbour web, additive blending, a small bright core, a layered halo, their own slow rotation (per-agent speed and phase, faster while active) and an own breathing offset. Idle satellites are dimmer and desaturated but still coloured; activity makes them vivid. Status is never colour-only: label text, a status ring, a spinner while a span is open, a dashed ring plus red tint on failure, and the duration chip stay.
- Agent colours come from a 12-colour palette defined in `activity-orb.js` (`AGENT_PALETTE`, dark-background friendly): cyan, violet, magenta, amber, lime, orange, sky, rose, teal, indigo, yellow, peach. An agent's preferred colour is fixed for known built-in ids (light amber, climate sky, cover indigo, vacuum teal, scene magenta, security rose, media violet, music cyan, automation orange, calendar lime, general yellow, timer peach, lists lime, send sky) and a stable FNV-1a hash into the palette for every other id. Visible slots never show the same colour: the colour is assigned when the agent enters a slot, and if another visible slot already holds the preferred colour the nearest unused palette colour (by palette order) is taken instead; it is kept while the agent stays in the slot and cross-fades when a slot is replaced. Reserved: the orchestrator stays theme blue (`--blue`), error red (`--danger`) is only used for failures, and the success green (`--success`) is only a brief ring flash on an ok return, never an agent identity. The colour tints points, web, core, halo, label, chip, link end, pulses (outbound and return) and the dispatch shockwave; the link is a straight gradient from the orchestrator colour to the agent colour; the tooltip shows a colour swatch. Label text is tinted toward white and keeps a contrast ratio of at least 6:1 against the stage background for every palette colour.
- Exactly 7 slots, rotated by last use (LRU). "Used" means a `span_start` of a dispatch-type span (`dispatch`, `dispatch_content`, `dispatch_send`, `dispatch_fallback`) for that agent; completions and phase spans do not count. An agent that already has a slot only has its last-used time refreshed. An event for an agent without a slot replaces the idle slot whose agent was used longest ago (empty slots first); a slot with an open span is never evicted, so with all 7 busy the event is not drawn.
- Seeding: `GET /api/admin/activity/recent?limit=7` (session cookie, `{"agents": [{"agent_id", "name", "last_used"}]}`, most recent first, already filtered to registered non-internal agents) fills the slots in that order, most recent being the most protected from eviction; `GET /api/admin/agents` supplies display names for agents not in that list. If the endpoint is missing or fails, the widget falls back to `GET /api/admin/overview/extended` (`agent_distribution` by count) and then to the agents list; the extra `overview/extended` request is only made in that fallback. Names come from the response, else the agents list, else the prettified id. Demo mode pads to 7 with well-known ids and now and then calls an agent that has no slot, so the rotation can be watched.
- Open spans are tracked by `span_id`; a lost `span_end` is released after the timeout, and all open spans are dropped after a reconnect. Several widgets on one page share one connection (refcounted, closed with the last widget).
- Connection indicator: `live`, `reconnecting`, `offline` (`demo` in demo mode). When the browser gives up (HTTP 401/429 closes an EventSource permanently), the widget retries itself with backoff 2 s doubling to 60 s, reset on the first open or message.

Behavior:

- Colors are read at runtime from the dashboard tokens (`--blue` sphere, `--teal-glow` active, `--success`, `--danger`, `--color-*` text) with dark fallbacks; they are re-read on `class`/`data-theme`/`style` changes of `<html>` and on a `agenthub:theme-change` window event. The dashboard is dark-only today, so no toggle exists yet. The widget CSS inside the shadow root is the one place where `var(--token, fallback)` is allowed, so the tag also works on pages without the token stylesheet.
- Labels and duration chips are drawn in a final pass above the sphere, links and halos, with a soft dark halo behind the glyphs; the depth dimming applies to the node only. A label sits outward from the ring centre (above for nodes in the upper half, below for the lower half, so the margins above and below the ring stay equal) and changes side (eased, with hysteresis) only when the node crosses the middle band or the other side is clearly freer, for example when its own side would put it on the sphere's glow extent, another node or the stage edge; the chip takes the opposite side. Measured over a full rotation at 1280, 768, 375 and 320 px, idle and all busy: no label overlaps the sphere's web, and text contrast against the brightest pixel behind it is at least 7.2:1 (7.7:1 with the halo).
- Round silhouette: the orchestrator is scaled uniformly (one radius for both axes, weak perspective), points sit on a quasi-uniform Fibonacci lattice, and the radial displacement is a tiny (at most about 2.4% of the radius), high-frequency perturbation. Ripple and shimmer are carried by brightness, not by the radius. Measured width/height of the point bounding box stays within 0.986-1.019 across idle, dispatch, phase and error states, resizes and every point-count tier. Low-frequency displacement (wavelength comparable to the sphere) must not be reintroduced, it turns the sphere into an oval.
- Overlays and centring: the connection badge (top right corner), the offline hint, the tooltip and the card's debug panel are `position: absolute` and take no layout space; the canvas fills the whole stage. The orchestrator's centre is the exact geometric centre of the stage (offset 0 px, measured in the overview widget, compact, the card with and without title, transparent, with height and width options, in a fixed cell and in the stage-size matrix). Because the perspective makes near satellites and their labels larger than far ones, a ring centred on the sphere leaves less room below than above; the solver therefore shifts the ring (not the sphere) by `ring.ox`/`ring.oy` (a few px sideways, 2 to 35 px upward on large stages) so that nodes plus labels leave equal margins at the top and bottom and at the left and right over a full rotation (measured within 1 px; for example 14 to 17 px at 1280, 768, 375 and 320).
- Orbit: the satellite ring has a fixed tilt and a fixed roll of 10 degrees (no wobble); only the satellites move along it. The tilt is solved per stage size (`_solveRing`, flattest ring that fits, typically 65 to 75 degrees on 16:10 or square stages, about 46 degrees on very large ones). The sphere's visual extent is `HALO_K` = `RAD_MAX` x `GLOW_K` = 1.14 x 1.25 = 1.425 times its static radius: 1.14 is the largest breathing/activity radius, and every glow (halo sprite, soft inner glow, outer glow ring) ends at 1.25 x the current radius (the energized ring sits at 1.12 x). The solver keeps every satellite disc (status ring and spinner included), far or near, at least `HALO_GAP` = 4 px outside that extent over a full rotation (measured minimum gap 4.0 to 4.9 px on stages up to 600 px and much more on large ones, in every viewport and the stage-size matrix), so the glow never reaches a satellite; only the dispatch shockwave ring may pass over nodes briefly. The sphere is therefore about 0.14 to 0.19 of the smaller side (13 to 16 percent smaller than before), with weak perspective (satellite scale about 0.9 to 1.12), and far-half satellites project above its silhouette instead of behind it. Depth order: far-half satellites are drawn before the sphere, near-half ones after it, flipping at the sides of the ring where the satellite is far outside the disc, so size and alpha stay continuous (about 0.05% per 0.0125 rad step); links start at the sphere surface and stay straight; labels and chips are drawn last.
- Any stage shape: layout derives from the real stage width and height (ResizeObserver, eased). If the full-size look does not give a sphere of at least 15% of the smaller side, the solver steps through smaller satellites and shorter labels (7, 5 characters) and finally hides the labels (names remain in the tooltip and for screen readers); it never lets labels or nodes overlap. Checked over 160x160, 240x160, 320x200, 600x150, 900x180, 150x450, 400x600, 1200x675 and 1280x800, idle and all busy, with no node or label overlap, no far node or label on the sphere and correct depth order.
- Smooth transitions: every state-dependent value (activity, energy, rotation speed, core and halo brightness, web brightness, spinner ring, node heat, error tint, chip and flash, satellite fade, colour) is driven by frame-rate independent critically damped smoothing toward a target (attack about 0.2 s, release about 0.6 to 0.9 s) and never switched or reset. Rotation integrates the eased angular speed, the breathing phase is a free-running clock that never resets, shockwaves fade in and out, slot replacement fades the old satellite out and the new one in while its colour cross-fades, and the connection indicator fades its colour. Event impulses are short target holds, not instant value writes.
- Start-up: on connect the widget fades in (about 0.7 s; the orb eases from slightly smaller and dimmer to its idle size) with every smoothed value starting at its idle target. The stage is measured with its final aspect class (narrow widgets switch to 1:1 before the first measurement), painting starts after the first measured size, a canvas resize repaints synchronously (never an empty frame) and later layout changes glide. Frame delta is capped at 50 ms. Satellites fade in only once the agent data is seeded, and demo mode waits for a calm idle phase before its first request. The adaptive quality logic has a 2.5 s grace period, and when it does step the point count down, the old and new point sets crossfade over 0.8 s (complementary weights, so brightness and silhouette do not pop); the point geometry is built on the first frame, not in the mount task (a card that never becomes visible never builds it): the first frames use a 120-point boot geometry, the real tier is built in time slices of about 3 ms and crossfaded in, and lower tiers are built the same way in the background, one every 250 ms.
- Idle: the whole sphere breathes about 5% in scale on a 4 s sine period while core and halo brightness swing roughly 40%, a ripple travels pole to pole through the surface once per breath, a faint brightening runs along each link from the sphere to its node, and nodes shimmer gently. A noise field of three drifting plane waves modulates point brightness (and, very slightly, radius).
- Performance: DPR capped at 2 (1.5 for the Lovelace card); frames are capped at about 62 fps, 30 fps when the viewport is under 600px or `hardwareConcurrency <= 4`, and 30 fps for card instances whenever more than one is running; the render loop starts only after the first IntersectionObserver callback and pauses while off-screen or `document.hidden`. Point count by tier (orchestrator 1200 desktop, 600 small or low-power, plus 90 or 52 points per satellite) with precomputed neighbour pairs (spatial-grid search, polyline chains, geometry and ring layout cached module-wide so several cards share them). If frames run long (JS work above 8 ms, or the frame interval 40% over target, for about 45 frames; a frame gap above 250 ms counts as a run of slow frames) the widget steps both counts down (desktop 1200/900/600/420/300 with 90/72/56/44/36 per satellite, small 600/450/330/240 with 52/44/36/30) and never back up. Per-frame work: the noise terms use precomputed phases (one sine per point), halos are cached glow sprites, satellite web and points are drawn in one pass, a calm orb reuses the point projection on every second frame in direct mode, and a short A/B probe (after 3.5 s) switches to a cached sphere layer redrawn every 2nd or 3rd frame only where that is cheaper (Firefox), never in Chrome where the direct path wins; the layer is composited with the same operator the sphere is drawn with (additive on the dark profile, source-over on the light one), so both paths render the same pixels. Layout, surface and colour refreshes are debounced (60 ms, 120 ms, 250 ms). Measured on the reference machine (1066x302 stage, 1x, tier 0): first card mounts in about 9 ms, three cards at 8x CPU throttle in about 180 ms, draw cost per executed frame about 0.7 ms in Chrome (was 1.1) and 2.6 ms in Firefox (was 5.1).
- `prefers-reduced-motion`: no rotation, breathing, noise motion, shockwaves, spinner or travelling particles (one static frame); activity is shown as brief node highlights and the render loop stops when nothing is changing.
- Accessibility: the canvas is focusable with an `aria-label`; Left/Right arrows step through agents and show the tooltip, Escape clears; hover (mouse) or tap (touch) shows a tooltip with name, id, session request count, last duration and status; a visually hidden `role="status"` region announces events: the first message after a quiet period is read at once, and messages inside a 1.5 s window are queued and read together (a burst collapses, nothing is dropped).
- Render profiles: on a dark surface (the default stage, a dark card or page) the sphere, web, halos and satellites use additive blending. Over a light surface (a light `stage-background`, or a transparent orb whose composed ancestor background is light) the widget switches to a source-over profile: hues are darkened until the strongest orb pixels keep at least 3:1 and labels, chips and the status text at least 4.5:1 against the detected surface, alpha is raised to compensate for the missing additive gain, and the change is hysteretic and eased (a short dip, never a pop). The surface is found by walking the composed ancestors up to a fixed depth; photographic backgrounds (a `background-image` behind a translucent layer) are not analysed, labels there get a stronger outline instead, so a photo may reduce contrast and a solid `stage-background` is the reliable choice. Detection re-runs on theme changes, `refreshSurface()` and a 2 s sweep, only for transparent or explicitly filled stages.

### Home Assistant Lovelace card

The same widget ships to Home Assistant as the Lovelace card `custom:agenthub-activity-orb-card`.

- Sources: the widget (`container/app/dashboard/static/activity-orb.js`) and the card wrapper (`container/app/dashboard/static/activity-orb-card.src.js`, not loaded by the dashboard). `python scripts/sync_orb_card.py` concatenates a generated-file banner (with the `<version>.<static build>` marker for `?v=` cache busting), the widget and the wrapper into the one file the integration registers, `custom_components/ha_agenthub/frontend/agenthub-activity-orb-card.js`. `--check` exits 1 with the fix command when that file is stale and runs in `scripts/ci.py`; rerun the script after any change to either source or to `VERSION` / `_STATIC_BUILD`.
- Config (all optional, unknown keys ignored, invalid types or sizes throw a clear `setConfig` error):

  | Key | Meaning |
  |-----|---------|
  | `title` | Heading, default `Agent Activity`; empty hides the heading |
  | `show_title` | `false` hides the heading row completely (no gap), default `true` |
  | `height` | Number (px) or CSS length (`320px`, `40vh`, `50%`); default automatic. Numbers are clamped to 160 to 4000 px, other units are clamped with CSS `min()`/`max()` so labels stay legible |
  | `width` | Number (px) or CSS length, applied as `max-width` with the card centred in its cell; default the full cell width |
  | `transparent` | `true` removes the dark stage fill and the card background, shadow and border (the corner radius is kept); the orb adapts to light and dark surfaces. Default `false` |
  | `background` | CSS colour for the stage fill, for example `rgba(0,0,0,0.4)` or `var(--card-background-color)`; wins over the dark default and over `transparent` for the stage (the card chrome still follows `transparent`). Invalid colours throw a clear `setConfig` error |
  | `compact`, `demo` | Booleans (compact only drops the extra spacing; the stage keeps its 16:10 / 1:1 shape) |
  | `debug` | `true` shows a compact monospace diagnostics panel inside the card, refreshed once a second (see Loading, healing and diagnostics); default `false`, nothing exists (no element, no timer) while off |
  | `entry_id` | Passed to the subscribe command; default the first loaded entry |

  Size precedence: an explicit `height` fixes the stage (and `getGridOptions` pins `rows`, `min_rows` and `max_rows` to the rows that height needs at about 56 px plus an 8 px gap, 12 columns, `min_columns` 6); otherwise a card inside a fixed-height sections-view grid cell fills the cell (height 100%, HA's resize handles work, `rows` 5, 4 without a title, `min_rows` 3, `max_rows` 9, compact 3, 2 and 6); otherwise the stage derives its height from the width. Percentage heights need a container with a definite height. Masonry: `getCardSize()` follows the same rules.
- Card picker and editor: the `window.customCards` entry (type without `custom:`, name, description, `preview: true`, `documentationURL`) makes the picker render a live element from `getStubConfig()`; the picker does not set `element.preview`, so the card treats a `preview` property, a `hui-card-picker` or `hui-card-preview` ancestor as a preview: demo mode with a short idle lead-in, never a subscription. The visual editor is `static getConfigForm()` (schema for HA's `hui-form-editor`: boolean selectors for `show_title`, `transparent`, `compact`, `demo`, `debug`; text selectors for `title`, `height`, `width`, `background`, `entry_id`, with helper texts; `height` and `width` are text fields because a number selector cannot express `40vh`, and plain numbers typed there are px). A `getConfigElement()` is deliberately not defined: HA prefers it over `getConfigForm()` when both exist, and `hacs.json` requires HA 2025.8.0, which supports `getConfigForm()`.
- After the connection has been offline for 5 s the widget shows a small hint inside the stage (`Waiting for HA-AgentHub`).
- WebSocket protocol: `hass.connection.subscribeMessage(cb, {type: "ha_agenthub/activity/subscribe", entry_id?})`. The bridge sends `{type: "snapshot", agents: [{agent_id, name, last_used}], connected}` first (mapped to `applySnapshot` and the indicator, live when connected and offline otherwise; `connected: false` there is not permanent), then `{type: "activity", event: <activity payload>}` (mapped to `pushEvent`) and `{type: "status", connected}` (live or offline). An error response (for example `not_loaded`, or an unknown command on an older bridge) shows `offline` and retries with backoff 5 s doubling to 120 s. HA websocket `disconnected` / `reconnect-error` show `reconnecting`; HA replays the subscription itself, so the card never double-subscribes. The card unsubscribes in `disconnectedCallback` and subscribes again in `connectedCallback`.
- Self-healing: a bridge config entry reload sends a final `status connected:false` and no further events while the client handle still looks alive, so a card that stays offline for 30 s unsubscribes and subscribes again.
- Theme: the card chrome and heading follow the HA theme; the stage is dark unless `transparent` or `background` is set (then the render profile follows the surface behind it). HA tokens are mapped onto the widget tokens as inline custom properties on the widget host: `--blue` (orchestrator) from `--info-color` (else `--primary-color`), lightened so it reads on dark; `--danger` from `--error-color`; `--success` from `--success-color`; `--color-mist` and `--color-dust` (label text) from `--secondary-text-color` and `--disabled-text-color`, only when the theme is dark (light themes have dark text that would vanish on the stage, so the widget defaults stay). The mapping is recomputed when `hass.themes` changes. All other widget tokens fall back to built-in dark values, which is the one place where `var(--token, fallback)` is intended.
- Loading, healing and diagnostics (`scripts/sync_orb_card.py` owns the gate, the widget and card sources own the containment):
  - Gate: the generated file defines both elements inside `run()`, which runs exactly once. The HA frontend replaces `window.customElements` with the scoped-custom-element-registry polyfill during boot, and a native define made earlier is invisible to Lovelace's `get()`/`whenDefined()`. Paths (recorded in `gatePath`): `immediate-polyfill` (polyfill already installed), `immediate-registry-ready` (the registry already knows `home-assistant`), `immediate-no-ha-root` (no `<home-assistant>` element: previews, harnesses), `immediate-unsupported`, otherwise `whenDefined` (`whenDefined('home-assistant')` resolved) or `polyfill-detected` (short timers saw the polyfill or a replaced `window.customElements`, which also covers a frontend that renames its root element) or `fallback` (30 s `setTimeout`; a late define is safe because HA rebuilds the card when `whenDefined` fires, an early one is the broken case).
  - Healing: whatever path defined the elements, short timers for 60 s (and `whenDefined('home-assistant')` on every registry seen) verify that `window.customElements.get(tag)` still finds both tags; a tag the current registry cannot see is defined again on it (`healing` events, `healed: true`). The orb ignores a duplicate `attributeChangedCallback` (same attribute and value), because the polyfill can deliver the change twice after such a second define.
  - Containment: nothing may throw into HA or the browser. Each source part runs in its own `try` block (a failing widget cannot stop the card from being defined; the card then shows "The Agent Activity Orb widget is not available"), the card and widget lifecycle methods, the `hass` and `preview` setters, `getCardSize`/`getGridOptions`, the render loop frame, SSE and subscription callbacks, observers (Resize, Intersection, Mutation), listeners, timers and async chains are wrapped, and a failure is recorded and logged with `console.error` once per distinct message. Three consecutive failed frames stop that widget's render loop (the last frame stays, other cards and the page continue). `setConfig` still throws for an invalid configuration (HA's contract for its config error card); a card whose constructor failed throws "failed to initialise" from `setConfig`.
  - Diagnostics: one `console.info` line when the module evaluates and one per defined element (build, gate path, timestamps). `window.__AGENTHUB_ORB_DIAG__` is a plain object: `build`, `gatePath`, `healed`, `evaluatedAt`, `t` (ms since page start: `evaluated`, `run`, `defined:<tag>`), `defined`, `polyfillAtDefine`, `healing` (events), `errors` (last 20: `t`, `where`, `message`, `stack`), `errorCounts`, and `cards` (one summary per card: `attached`, `demo`, `subscription`, `connection`, `offlineSince`, `lastError`, `renderStopped`, `orbReady`, `broken`, `debug`). Copy it from the browser console with `JSON.stringify(window.__AGENTHUB_ORB_DIAG__, null, 1)`.
  - Debug panel (`debug: true`): build, gate path, healed, module and define times, polyfill present, `customElements.get` result for both tags, subscription state, connection state and offline duration, frames per second, tier, point count, render stopped marker and the last two errors.
- The card registers in `window.customCards` (guarded against double registration) and the file is safe to evaluate in the HA frontend: it touches no dashboard globals and opens nothing until an element connects.

## Anti-patterns

1. **Do not introduce fixed inline styles for padding, margin, or colour.** Use utility classes or reusable CSS rules for static presentation.
2. **Do not introduce CSS variables with fallback values** (e.g. `var(--token, #fallback)`). All tokens are defined in `tokens.css`.
3. **Do not introduce raw hex colours in templates.** Always use `var(--token)`.
4. **Do not introduce a CSS framework or bundler** in this project.
5. **Do not add light-mode styling** without a roadmap entry in `TODO.md`.
6. **Limit Alpine `:style` to calculated numeric geometry** that cannot be represented by reusable classes, such as data-driven chart heights and timeline positions. Use `:class` for states and variants; keep static presentation in CSS and never interpolate user-supplied CSS or colours.

## Adding a New Token

1. Add to `:root` in `tokens.css` under the appropriate comment group.
2. Document in this guide.
3. Increment `_STATIC_BUILD` in `app/dashboard/static_assets.py`.

## Adding a New Component

1. Add the rule(s) under an appropriate section in `components.css` (or `pages/*.css` if page-specific).
2. Document in this guide with a minimal HTML example.
3. Demonstrate in at least one template.
