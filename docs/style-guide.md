# UI Style Guide

This document describes the visual conventions for the HA-AgentHub admin dashboard and setup wizard.

## Scope and Intent

The admin UI is dark-only, server-rendered with Jinja2, and styled with a single plain CSS file (`container/app/dashboard/static/style.css`). No CSS bundler, npm, or Tailwind is used. All shared styling lives in `style.css`; page-specific overrides are kept minimal and documented.

## Design Tokens

Tokens are defined in the `:root` block of `style.css`.

### Surfaces

| Token | Value | Usage |
|-------|-------|-------|
| `--bg-void` | `#0c1017` | Deepest background (body) |
| `--bg-obsidian` | `#111722` | Input backgrounds, sidebar |
| `--bg-basalt` | `#171e2c` | Card surfaces |
| `--bg-charcoal` | `#1e2738` | Elevated panels |
| `--bg-stone` | `#283348` | Borders, dividers |

### Text

| Token | Value | Usage |
|-------|-------|-------|
| `--color-ash` | `#354563` | Muted borders |
| `--color-dust` | `#4d5f7e` | Labels, hints |
| `--color-fog` | `#7e8da6` | Secondary text |
| `--color-mist` | `#a8b4c8` | Tertiary text |
| `--color-cloud` | `#ccd4e2` | Body text on dark panels |
| `--color-light` | `#e4e9f1` | Primary text |
| `--color-bright` | `#f2f4f8` | Headings |

### Accent

| Token | Value | Usage |
|-------|-------|-------|
| `--teal` | `#2dd4bf` | Primary accent |
| `--teal-dim` | `#14b8a6` | Hover states |
| `--teal-glow` | `#0d9488` | Active/focus |
| `--teal-pale` | `#99f6e4` | Glows |

### Semantic

| Token | Value | Usage |
|-------|-------|-------|
| `--success` / `--sage` | `#6dba88` | Success states |
| `--warning` / `--soft-amber` | `#f59e0b` | Warnings |
| `--danger` / `--coral` | `#e87777` | Errors, destructive actions |
| `--danger-strong` / `--ember` | `#d44545` | Strong error emphasis |
| `--info` / `--blue` | `#60a5fa` | Information |

### Borders

| Token | Value | Usage |
|-------|-------|-------|
| `--border` | `var(--bg-stone)` | Default borders |
| `--border-subtle` | `rgba(255,255,255,0.06)` | Subtle dividers |
| `--border-strong` | `var(--color-ash)` | Emphasised dividers |

### Inputs

| Token | Value | Usage |
|-------|-------|-------|
| `--input-bg` | `var(--bg-obsidian)` | Input backgrounds |

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
| `--radius-md` | `0.5rem` |
| `--radius-lg` | `0.75rem` |
| `--radius-pill` | `999px` |

### Typography

| Token | Stack |
|-------|-------|
| `--font-body` | `'DM Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif` |
| `--font-display` | `'Outfit', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` |
| `--font-mono` | `"SF Mono", "Fira Code", "Fira Mono", Menlo, Consolas, monospace` |

## Component Classes

### Card

```html
<div class="card">
    <h3 class="card-title">Title</h3>
    <p class="card-subtitle">Subtitle</p>
    <div class="card-body">Content</div>
</div>
```

Modifiers: `.card-narrow` (max-width 600px).

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

### Alert

```html
<div class="alert alert-success">...</div>
<div class="alert alert-error">...</div>
<div class="alert alert-info">...</div>
<div class="alert alert-warning">...</div>
```

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

Group buttons with `.btn-group`.

### Form

```html
<div class="form-group">
    <label class="form-label">Label</label>
    <input type="text" class="form-input">
</div>
```

Variants: `.form-select`, `.form-textarea`.
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
| `.pr-md` | `padding-right: 1rem` |
| `.pt-md` | `padding-top: 1rem` |
| `.pb-md` | `padding-bottom: 1rem` |
| `.py-md` | `padding-top: 1rem; padding-bottom: 1rem` |

### Layout

| Class | Value |
|-------|-------|
| `.flex-1` | `flex: 1` |
| `.flex-none-w-180` | `flex: 0 0 180px` |
| `.flex-wrap` | `flex-wrap: wrap` |
| `.gap-xs` | `gap: 0.25rem` |
| `.gap-sm` | `gap: 0.5rem` |
| `.gap-md` | `gap: 1rem` |
| `.justify-start` | `justify-content: flex-start` |
| `.justify-end` | `justify-content: flex-end` |
| `.items-start` | `align-items: flex-start` |
| `.items-end` | `align-items: flex-end` |
| `.self-end` | `align-self: flex-end` |
| `.col-span-3` | `grid-column: span 3` |

### Width / Height

| Class | Value |
|-------|-------|
| `.w-auto` | `width: auto` |
| `.w-120` | `width: 120px` |
| `.w-140` | `width: 140px` |
| `.w-200` | `width: 200px` |
| `.w-full` | `width: 100%` |
| `.min-w-180` | `min-width: 180px` |
| `.min-w-200` | `min-width: 200px` |
| `.min-w-220` | `min-width: 220px` |
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
| `.bg-obsidian` | `background: var(--bg-obsidian)` |
| `.bg-teal-10` | `background: rgba(94,234,212,0.10)` |
| `.bg-amber-10` | `background: rgba(251,191,36,0.10)` |
| `.bg-green-10` | `background: rgba(134,239,172,0.10)` |
| `.bg-blue-10` | `background: rgba(96,165,250,0.10)` |
| `.border-right-subtle` | `border-right: 1px solid rgba(255,255,255,0.06)` |
| `.rounded-md` | `border-radius: 0.5rem` |
| `.hidden` | `display: none` |

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

## JavaScript Helpers

| Helper | Purpose | Example |
|--------|---------|---------|
| `dashboardApi.request(url, options)` | Authenticated fetch wrapper | `await dashboardApi.json('/api/admin/settings')` |
| `dashModal()` | Alpine modal factory with focus trap | `x-data="dashModal()"` |
| `dashToasts()` | Alpine toast queue factory | Used by `#toast-root` |
| `window.toast(msg, kind)` | Push a toast notification | `window.toast('Saved', 'success')` |
| `window.chartColors()` | Read design tokens from CSS | `chartColors().teal` |
| `window.chartRgba(token, alpha)` | Convert CSS token to rgba | `chartRgba('--teal', 0.5)` |

## Anti-patterns

1. **Do not introduce inline styles for padding, margin, or colour.** Use the utility classes or add a reusable class to `style.css`.
2. **Do not introduce CSS variables with fallback values** (e.g. `var(--token, #fallback)`). All tokens are defined in `:root`.
3. **Do not introduce raw hex colours in templates.** Always use `var(--token)`.
4. **Do not introduce a CSS framework or bundler** in this project.
5. **Do not add light-mode styling** without a roadmap entry in `docs/roadmap.md`.

## Adding a New Token

1. Add to `:root` in `style.css` under the appropriate comment group.
2. Document in this guide.
3. Bump `style.css?v=N` in `dashboard_base.html`, `base.html`, and `base_setup.html`.

## Adding a New Component

1. Add the rule(s) under an appropriate section in `style.css`.
2. Document in this guide with a minimal HTML example.
3. Demonstrate in at least one template.
