/*
 * agenthub-activity-orb-card - Home Assistant Lovelace card around <agenthub-activity-orb>.
 *
 * Source of the card wrapper. It is NOT loaded by the container dashboard: scripts/sync_orb_card.py
 * concatenates activity-orb.js and this file into the single deliverable
 * custom_components/ha_agenthub/frontend/agenthub-activity-orb-card.js (the widget must be defined first).
 *
 * Plain HTMLElement with Shadow DOM (LitElement is not available in a custom card). Data arrives over the
 * Home Assistant WebSocket through the bridge command "ha_agenthub/activity/subscribe":
 *   {type: "snapshot", agents: [{agent_id, name, last_used}], connected: bool}   first message
 *   {type: "activity", event: <activity payload>}                                 span_start / span_end
 *   {type: "status", connected: bool}                                             container link state
 * Config (all optional): title (string, default "Agent Activity"; empty hides it), show_title (boolean, default true),
 * height (number = px or CSS length; default auto), width (number = px or CSS length, max-width, centred),
 * transparent (boolean: no stage fill and no card chrome), background (CSS colour for the stage fill; wins over the
 * default dark stage), compact (boolean), demo (boolean), entry_id (string, passed to the subscribe command to pick a config entry;
 * without it the bridge uses its first loaded entry). Unknown keys are ignored.
 * Size precedence: an explicit height fixes the stage height; otherwise a card inside a fixed-height grid cell
 * (sections view) fills the cell; otherwise the stage derives its height from the width (16:10, 1:1 when narrow).
 *
 * Card picker and editor (verified against the Home Assistant frontend sources):
 *  - window.customCards entry {type, name, description, preview, documentationURL}: the picker lists it, adds the
 *    "custom:" prefix itself and, with preview true, renders a live element (stub config from getStubConfig).
 *  - The picker does not set element.preview; it renders the element inside hui-card-picker. A preview is therefore
 *    detected by the preview property (set by hui-card, e.g. in the edit dialog) or by a hui-card-picker or
 *    hui-card-preview ancestor in the composed tree. Previews run the widget in demo mode and never subscribe.
 *  - static getConfigForm() returns the schema for HA's own hui-form-editor (min HA 2025.8.0 supports it). A
 *    getConfigElement() is deliberately not defined: HA prefers it over getConfigForm when both exist.
 *
 * Self-healing: when the bridge reloads its config entry it sends {status connected:false} and then nothing, while
 * the client-side subscription still looks alive. If the card stays offline for OFFLINE_RESUBSCRIBE_MS it
 * unsubscribes and subscribes again; an error response (for example not_loaded) is retried with backoff.
 */
(function () {
    'use strict';

    var CARD_TAG = 'agenthub-activity-orb-card';
    if (typeof window === 'undefined' || !window.customElements || window.customElements.get(CARD_TAG)) {
        return;
    }

    var SUBSCRIBE_MESSAGE = { type: 'ha_agenthub/activity/subscribe' };
    var BACKOFF_START_MS = 5000;
    var BACKOFF_MAX_MS = 120000;
    var OFFLINE_RESUBSCRIBE_MS = 30000;

    var STYLE = [
        ':host { display: block; height: 100%; }',
        'ha-card { overflow: hidden; height: 100%; display: flex; flex-direction: column; box-sizing: border-box; }',
        'ha-card.fixed { height: auto; }',
        '.header { padding: 12px 16px 0; font-family: var(--ha-card-header-font-family, inherit);',
        '  font-size: var(--ha-card-header-font-size, 24px); font-weight: var(--ha-font-weight-normal, 400);',
        '  line-height: var(--ha-line-height-condensed, 32px); letter-spacing: -0.012em; color: var(--ha-card-header-color, var(--primary-text-color));',
        '  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
        '.header[hidden] { display: none; }',
        '.body { padding: 12px 16px 16px; flex: 1; min-height: 0; min-width: 0; }',
        '.body.no-title { padding-top: 16px; }',
        'agenthub-activity-orb { display: block; width: 100%; min-width: 0; }'
    ].join('\n');

    // Colour of an HA theme variable as [r, g, b], or null when unresolved.
    function resolveColor(host, name) {
        var raw = getComputedStyle(host).getPropertyValue(name).trim();
        if (!raw) { return null; }
        try {
            var c = document.createElement('canvas').getContext('2d');
            c.fillStyle = '#000001';
            c.fillStyle = raw;
            var v = c.fillStyle;
            if (v.charAt(0) === '#' && v.length === 7) {
                return [parseInt(v.slice(1, 3), 16), parseInt(v.slice(3, 5), 16), parseInt(v.slice(5, 7), 16)];
            }
            var m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/.exec(v);
            if (m) { return [+m[1], +m[2], +m[3]]; }
        } catch (_) { /* unresolved */ }
        return null;
    }

    function luminance(rgb) {
        return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255;
    }

    function mixWhite(rgb, t) {
        return 'rgb(' + rgb.map(function (v) { return Math.round(v + (255 - v) * t); }).join(',') + ')';
    }

    var LENGTH_UNITS = ['px', '%', 'vh', 'vw', 'vmin', 'vmax', 'rem', 'em', 'cm', 'mm'];
    var MIN_SIZE_PX = 160;
    var MAX_SIZE_PX = 4000;

    // Parse a size option. Returns null for "automatic", else {css, px}: css is always safe to put in a style
    // (clamped to 160 to 4000 px; relative units are clamped with CSS min()/max()), px is the number for px values.
    function parseLength(value, name) {
        if (value === undefined || value === null || value === '' || value === 'auto') { return null; }
        var hint = '"' + name + '" must be a positive number (px) or a CSS length such as "320px", "40vh" or "50%"';
        var num, unit;
        if (typeof value === 'number') {
            if (!isFinite(value) || value <= 0) { throw new Error(hint); }
            num = value; unit = 'px';
        } else if (typeof value === 'string') {
            var m = /^\s*(\d+(?:\.\d+)?)\s*([a-z%]*)\s*$/i.exec(value);
            if (!m) { throw new Error(hint); }
            num = parseFloat(m[1]);
            unit = (m[2] || 'px').toLowerCase();
            if (LENGTH_UNITS.indexOf(unit) < 0) {
                throw new Error('"' + name + '" has an unknown unit "' + m[2] + '" (use px, %, vh, vw, vmin, vmax, rem, em, cm or mm)');
            }
            if (!(num > 0)) { throw new Error(hint); }
        } else {
            throw new Error(hint);
        }
        if (unit === 'px') {
            var px = Math.min(MAX_SIZE_PX, Math.max(MIN_SIZE_PX, num));
            return { css: px + 'px', px: px };
        }
        return { css: 'min(' + MAX_SIZE_PX + 'px, max(' + MIN_SIZE_PX + 'px, ' + num + unit + '))', px: null };
    }

    function validateConfig(config) {
        if (!config || typeof config !== 'object' || Array.isArray(config)) { throw new Error('Invalid configuration'); }
        var opt = function (key, type) {
            var v = config[key];
            if (v !== undefined && v !== null && typeof v !== type) { throw new Error('"' + key + '" must be a ' + (type === 'string' ? 'string' : 'boolean')); }
        };
        opt('title', 'string');
        opt('show_title', 'boolean');
        opt('compact', 'boolean');
        opt('demo', 'boolean');
        opt('entry_id', 'string');
        opt('transparent', 'boolean');
        if (config.background !== undefined && config.background !== null) {
            if (typeof config.background !== 'string' ||
                (typeof CSS !== 'undefined' && CSS.supports && config.background.trim() !== '' && !CSS.supports('color', config.background))) {
                throw new Error('"background" must be a CSS colour such as "rgba(0,0,0,0.4)" or "var(--card-background-color)"');
            }
        }
        parseLength(config.height, 'height');
        parseLength(config.width, 'width');
    }

    class AgentHubActivityOrbCard extends HTMLElement {
        constructor() {
            super();
            this._config = null;
            this._hass = null;
            this._conn = null;
            this._unsub = null;
            this._subscribing = false;
            this._token = 0;
            this._retryTimer = 0;
            this._offlineTimer = 0;
            this._backoff = BACKOFF_START_MS;
            this._attached = false;
            this._themeSig = '';
            this._previewProp = false;
            this._connListeners = null;
            var root = this.attachShadow({ mode: 'open' });
            var style = document.createElement('style');
            style.textContent = STYLE;
            root.appendChild(style);
            this._card = document.createElement('ha-card');
            this._header = document.createElement('div');
            this._header.className = 'header';
            var body = document.createElement('div');
            body.className = 'body';
            this._body = body;
            this._orb = document.createElement('agenthub-activity-orb');
            this._orb.setAttribute('transport', 'external');
            this._orb.setAttribute('bare', '');
            body.appendChild(this._orb);
            this._card.appendChild(this._header);
            this._card.appendChild(body);
            root.appendChild(this._card);
        }

        /* ------------------------------ Lovelace card API ------------------------------ */

        static getStubConfig() {
            return { title: 'Agent Activity' };
        }

        // Schema-driven editor: HA renders it with ha-form inside hui-form-editor and fires config-changed itself.
        // height and width are text fields because a number selector cannot express "40vh" or "50%"; plain numbers
        // typed there are pixels.
        static getConfigForm() {
            var labels = {
                title: 'Title', show_title: 'Show title', height: 'Height', width: 'Width',
                transparent: 'Transparent background', background: 'Stage background',
                compact: 'Compact', demo: 'Demo mode', entry_id: 'Config entry ID'
            };
            var helpers = {
                title: 'Card heading. Ignored while Show title is off; leave empty to hide it as well.',
                show_title: 'Turn off to hide the heading row completely.',
                height: 'px or CSS length such as 320, 320px, 40vh or 50%. Empty = automatic (derived from the width, or the grid cell in the sections view). Minimum about 160 px.',
                width: 'px or CSS length such as 420 or 80%. The card is centred in its cell. Empty = full width of the cell.',
                transparent: 'Remove the dark stage and the card background so the orb floats on the dashboard. It adapts to light and dark surfaces.',
                background: 'Optional CSS colour for the stage fill, for example rgba(0,0,0,0.4) or var(--card-background-color). Wins over the dark default; Transparent background is the same as an empty stage fill.',
                compact: 'Smaller card without the extra spacing.',
                demo: 'Play simulated activity instead of live data (no connection to HA-AgentHub is used).',
                entry_id: 'Optional. Only needed with several HA-AgentHub config entries; by default the first loaded entry is used.'
            };
            return {
                schema: [
                    { name: 'show_title', selector: { boolean: {} } },
                    { name: 'title', selector: { text: {} } },
                    { name: 'height', selector: { text: {} } },
                    { name: 'width', selector: { text: {} } },
                    { name: 'transparent', selector: { boolean: {} } },
                    { name: 'background', selector: { text: {} } },
                    { name: 'compact', selector: { boolean: {} } },
                    { name: 'demo', selector: { boolean: {} } },
                    { name: 'entry_id', selector: { text: {} } }
                ],
                computeLabel: function (schema) { return labels[schema.name]; },
                computeHelper: function (schema) { return helpers[schema.name]; },
                assertConfig: function (config) { validateConfig(config); }
            };
        }

        // Unknown keys are ignored (YAML-only options never break the editor); invalid types and sizes throw.
        setConfig(config) {
            validateConfig(config);
            var prev = this._config;
            var height = parseLength(config.height, 'height');
            var width = parseLength(config.width, 'width');
            this._config = {
                title: config.title !== undefined && config.title !== null ? config.title : 'Agent Activity',
                show_title: config.show_title !== false,
                compact: config.compact === true,
                demo: config.demo === true,
                transparent: config.transparent === true,
                background: typeof config.background === 'string' ? config.background.trim() : '',
                entry_id: config.entry_id || '',
                height: height,
                width: width
            };
            var cfg = this._config;
            var titleVisible = cfg.show_title && cfg.title !== '';
            this._header.textContent = cfg.title;
            this._header.hidden = !titleVisible;
            this._body.classList.toggle('no-title', !titleVisible);
            this._orb.toggleAttribute('compact', cfg.compact);
            // Explicit height: fixed stage (wins over a grid cell); explicit width: max-width, centred in the cell.
            this._card.classList.toggle('fixed', !!height);
            this._orb.toggleAttribute('fixed-height', !!height);
            if (height) { this._orb.style.setProperty('--orb-stage-height', height.css); } else { this._orb.style.removeProperty('--orb-stage-height'); }
            // Transparent: no stage fill and no card chrome (background, shadow, border; the radius is kept).
            // background: an explicit stage fill that wins over both the dark default and transparent for the stage.
            this._orb.toggleAttribute('transparent', cfg.transparent && !cfg.background);
            if (cfg.background) { this._orb.setAttribute('stage-background', cfg.background); } else { this._orb.removeAttribute('stage-background'); }
            var chrome = cfg.transparent;
            this._card.style.setProperty('--ha-card-background', chrome ? 'transparent' : '');
            if (!chrome) { this._card.style.removeProperty('--ha-card-background'); }
            if (chrome) {
                this._card.style.setProperty('--ha-card-box-shadow', 'none');
                this._card.style.setProperty('--ha-card-border-width', '0');
            } else {
                this._card.style.removeProperty('--ha-card-box-shadow');
                this._card.style.removeProperty('--ha-card-border-width');
            }
            this._card.style.maxWidth = width ? width.css : '';
            this._card.style.marginLeft = width ? 'auto' : '';
            this._card.style.marginRight = width ? 'auto' : '';
            this._refreshMode(prev && prev.entry_id !== cfg.entry_id);
        }

        getCardSize() {
            var cfg = this._config;
            if (cfg && cfg.height && cfg.height.px) { return Math.max(2, Math.ceil((cfg.height.px + (cfg.show_title && cfg.title !== '' ? 48 : 0) + 28) / 50)); }
            return cfg && cfg.compact ? 3 : 5;
        }

        // Sections view: rows from the configured height (grid row about 56 px plus an 8 px gap); an explicit px
        // height pins the rows, otherwise HA's own sizing (resize handles, layout options) stays in charge and the card
        // fills the cell.
        getGridOptions() {
            var cfg = this._config || {};
            var titleRow = cfg.show_title !== false && cfg.title !== '' ? 48 : 0;
            if (cfg.height && cfg.height.px) {
                var rows = Math.min(12, Math.max(2, Math.ceil((cfg.height.px + titleRow + 28 + 8) / 64)));
                return { columns: 12, rows: rows, min_columns: 6, max_columns: 12, min_rows: rows, max_rows: rows };
            }
            if (cfg.compact) {
                return { columns: 12, rows: titleRow ? 3 : 2, min_columns: 6, max_columns: 12, min_rows: 2, max_rows: 6 };
            }
            return { columns: 12, rows: titleRow ? 5 : 4, min_columns: 6, max_columns: 12, min_rows: 3, max_rows: 9 };
        }

        // HA sets this on cards it renders as a preview (hui-card with preview true).
        set preview(value) {
            var next = value === true;
            if (next === this._previewProp) { return; }
            this._previewProp = next;
            this._refreshMode(false);
        }

        get preview() { return this._previewProp === true; }

        // True inside the card picker or a card preview (composed ancestors, shadow roots included).
        _inPreviewTree() {
            var n = this, hops = 0;
            while (n && hops++ < 60) {
                if (n.nodeType === 1 && /^hui-card-(picker|preview)$/i.test(n.tagName)) { return true; }
                n = n.parentNode || n.host || null;
            }
            return false;
        }

        // Demo mode applies to config.demo and to every preview (the picker must always show something and
        // must not open a bridge subscription).
        _demoMode() {
            return !!(this._config && this._config.demo) || this._previewProp || (this._attached && this._inPreviewTree());
        }

        _refreshMode(resubscribe) {
            if (!this._config) { return; }
            var demo = this._demoMode();
            var preview = this._previewProp || (this._attached && this._inPreviewTree());
            var was = this._modeDemo;
            this._modeDemo = demo;
            this._orb.toggleAttribute('demo', demo);
            // A preview should show activity within a few seconds, not after the calm idle lead-in of a normal demo.
            if (preview && !this._config.demo) { this._orb.setAttribute('demo-idle', '2'); } else { this._orb.removeAttribute('demo-idle'); }
            if (this._attached && (was !== demo || resubscribe)) {
                this._teardown();
                this._setup();
            }
        }

        set hass(hass) {
            this._hass = hass;
            var sig = hass && hass.themes ? String(hass.themes.theme) + '|' + String(hass.themes.darkMode) : '';
            if (sig !== this._themeSig) {
                this._themeSig = sig;
                // HA applies the new theme variables right after assigning hass.
                var self = this;
                requestAnimationFrame(function () { self._applyTheme(); });
            }
            if (this._attached) { this._setup(); }
        }

        get hass() { return this._hass; }

        connectedCallback() {
            this._attached = true;
            this._applyTheme();
            this._refreshMode(false);
            this._setup();
        }

        disconnectedCallback() {
            this._attached = false;
            this._teardown();
        }

        /* ------------------------------ theme mapping ------------------------------ */

        // The orb is designed for a dark stage: the card chrome follows the HA theme, the stage stays dark.
        // HA tokens are mapped onto the widget's tokens as inline custom properties on the widget host:
        //   --blue (orchestrator)  <- --info-color, else --primary-color, lightened so it reads on dark
        //   --danger (errors)      <- --error-color
        //   --success (ok flash)   <- --success-color
        //   --color-mist / --color-dust (label text) <- --secondary-text-color / --disabled-text-color,
        //                            only when the theme is dark (light themes have dark text, unreadable on the stage)
        _applyTheme() {
            var host = this._orb;
            if (!host) { return; }
            var set = function (name, value) {
                if (value) { host.style.setProperty(name, value); } else { host.style.removeProperty(name); }
            };
            var info = resolveColor(this, '--info-color') || resolveColor(this, '--primary-color');
            set('--blue', info ? mixWhite(info, luminance(info) < 0.55 ? 0.4 : 0.15) : '');
            var err = resolveColor(this, '--error-color');
            set('--danger', err ? mixWhite(err, luminance(err) < 0.4 ? 0.2 : 0) : '');
            var ok = resolveColor(this, '--success-color');
            set('--success', ok ? mixWhite(ok, luminance(ok) < 0.4 ? 0.2 : 0) : '');
            var secondary = resolveColor(this, '--secondary-text-color');
            var disabled = resolveColor(this, '--disabled-text-color');
            var darkTheme = secondary && luminance(secondary) > 0.5;
            set('--color-mist', darkTheme ? 'rgb(' + secondary.join(',') + ')' : '');
            set('--color-dust', darkTheme && disabled && luminance(disabled) > 0.3 ? 'rgb(' + disabled.join(',') + ')' : '');
            if (typeof host.refreshSurface === 'function') { host.refreshSurface(); }
        }

        /* ------------------------------ subscription ------------------------------ */

        _setup() {
            var cfg = this._config;
            if (!cfg || !this._attached) { return; }
            if (this._demoMode()) { return; }
            var hass = this._hass;
            if (!hass || !hass.connection) { return; }
            if (this._conn !== hass.connection) {
                // A new connection object (full reload): drop the old subscription and listeners first.
                this._teardown();
                this._conn = hass.connection;
                this._listenConnection(hass.connection);
            }
            if (this._unsub || this._subscribing || this._retryTimer) { return; }
            this._subscribe();
        }

        _listenConnection(conn) {
            if (!conn || typeof conn.addEventListener !== 'function') { return; }
            var self = this;
            var onDisconnected = function () { self._setState('reconnecting'); };
            var onReconnectError = function () { self._setState('reconnecting'); };
            // On "ready" HA has replayed the subscription; the bridge then sends a fresh snapshot (state -> live).
            conn.addEventListener('disconnected', onDisconnected);
            conn.addEventListener('reconnect-error', onReconnectError);
            this._connListeners = { conn: conn, onDisconnected: onDisconnected, onReconnectError: onReconnectError };
        }

        _subscribe() {
            var self = this;
            var token = this._token;
            this._subscribing = true;
            var promise;
            var command = { type: SUBSCRIBE_MESSAGE.type };
            if (this._config && this._config.entry_id) { command.entry_id = this._config.entry_id; }
            try {
                promise = this._conn.subscribeMessage(function (msg) { self._onMessage(msg); }, command);
            } catch (err) {
                promise = Promise.reject(err);
            }
            Promise.resolve(promise).then(function (unsub) {
                self._subscribing = false;
                if (token !== self._token || !self._attached) {
                    // Detached (or reconfigured) while the command was in flight: undo it.
                    if (typeof unsub === 'function') { try { unsub(); } catch (_) { /* ignore */ } }
                    return;
                }
                self._unsub = unsub;
            }, function () {
                self._subscribing = false;
                if (token !== self._token || !self._attached) { return; }
                // Error response (unknown command on an older bridge, not_loaded, ...): stay offline and retry with backoff.
                self._setState('offline', true);
                var delay = self._backoff;
                self._backoff = Math.min(BACKOFF_MAX_MS, self._backoff * 2);
                self._retryTimer = setTimeout(function () {
                    self._retryTimer = 0;
                    self._setup();
                }, delay);
            });
        }

        _onMessage(msg) {
            if (!msg || typeof msg !== 'object') { return; }
            this._backoff = BACKOFF_START_MS;
            if (msg.type === 'snapshot') {
                this._orb.applySnapshot({ agents: Array.isArray(msg.agents) ? msg.agents : [] });
                // connected:false in a snapshot is never permanent: a status message follows when upstream connects.
                this._setState(msg.connected === false ? 'offline' : 'live');
            } else if (msg.type === 'activity') {
                if (msg.event) { this._orb.pushEvent(msg.event); }
            } else if (msg.type === 'status') {
                this._setState(msg.connected ? 'live' : 'offline');
            }
        }

        // Drive the widget indicator; an offline state that lasts too long triggers a resubscribe (self-healing).
        _setState(state, fromRetryPath) {
            this._orb.setConnectionState(state);
            if (state === 'offline' && !fromRetryPath) {
                if (!this._offlineTimer) {
                    var self = this;
                    this._offlineTimer = setTimeout(function () {
                        self._offlineTimer = 0;
                        self._resubscribe();
                    }, OFFLINE_RESUBSCRIBE_MS);
                }
            } else if (this._offlineTimer) {
                clearTimeout(this._offlineTimer);
                this._offlineTimer = 0;
            }
        }

        // The server side subscription may be gone while the client handle still looks alive
        // (bridge config entry reload): drop it and subscribe again.
        _resubscribe() {
            if (!this._attached || !this._config || this._demoMode() || !this._conn) { return; }
            this._unsubscribe();
            this._subscribe();
        }

        _unsubscribe() {
            this._token += 1;
            if (this._retryTimer) { clearTimeout(this._retryTimer); this._retryTimer = 0; }
            if (this._offlineTimer) { clearTimeout(this._offlineTimer); this._offlineTimer = 0; }
            if (this._unsub) {
                var unsub = this._unsub;
                this._unsub = null;
                try {
                    var r = unsub();
                    if (r && typeof r.catch === 'function') { r.catch(function () { /* ignore */ }); }
                } catch (_) { /* ignore */ }
            }
            this._subscribing = false;
        }

        _teardown() {
            this._unsubscribe();
            this._backoff = BACKOFF_START_MS;
            var l = this._connListeners;
            if (l && l.conn && typeof l.conn.removeEventListener === 'function') {
                l.conn.removeEventListener('disconnected', l.onDisconnected);
                l.conn.removeEventListener('reconnect-error', l.onReconnectError);
            }
            this._connListeners = null;
            this._conn = null;
        }
    }

    window.customElements.define(CARD_TAG, AgentHubActivityOrbCard);

    window.customCards = window.customCards || [];
    if (!window.customCards.some(function (c) { return c && c.type === CARD_TAG; })) {
        window.customCards.push({
            type: CARD_TAG,
            name: 'Agent Activity Orb',
            description: 'Live view of the HA-AgentHub orchestrator and the seven agents it used most recently, with pulses for every dispatch.',
            preview: true,
            documentationURL: 'https://github.com/mainzerp/ha-agenthub/blob/main/docs/deployment.md#lovelace-activity-card'
        });
    }
})();
