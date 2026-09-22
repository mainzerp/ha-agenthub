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

## Sidebar IA

The sidebar is data-driven from `app.dashboard.nav_config.NAV_GROUPS`. Five collapsible groups:

1. **Operate** — Overview, Chat, System Health, Analytics, Traces
2. **Configure** — Agents, Custom Agents, Personality, Entity Index, MCP Servers, Plugins
3. **Domain Data** — Send Devices, Timers, Calendar, Persons
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
| `window.dashboardApi` | Authenticated fetch wrapper | `await dashboardApi.json('/api/admin/settings')` |
| `window.dashUrl()` | `root_path`-aware URL builder | `dashUrl('/dashboard/agents')` |
| `window.chartColors()` | Read named design colors from CSS | `chartColors().teal` |
| `window.chartRgba(token, alpha)` | Convert a named chart color to rgba | `chartRgba('teal', 0.5)` |
| `window.dashFormatRelativeTime(ts)` | Human-readable relative time | `dashFormatRelativeTime(Date.now())` |
| `window.dashFormatBytes(n)` | Human-readable bytes | `dashFormatBytes(1024)` |
| `window.dashTruncate(s, n)` | Truncate with ellipsis | `dashTruncate('hello world', 8)` |

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
